from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import aiohttp
import websockets

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry

log = logging.getLogger(__name__)


class BinanceFeed(BaseModule):
    """Real-time Binance market data feed.

    Subscribes to @depth and @aggTrade streams for every enabled
    symbol that has a `binance` mapping. Maintains a local order
    book per symbol, publishes on `feed.depth` and `feed.trade`.

    Supports pause/resume for isolated pipeline testing:
    - cmd_pause_feed  → stops publishing (WS still receives events)
    - cmd_resume_feed → resumes publishing
    """

    name = "feed.binance"

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._paused = False
        self._http: aiohttp.ClientSession | None = None
        self._books: dict[str, dict[str, Any]] = {}
        self._buffers: dict[str, list[dict]] = {}
        self._depth_count: dict[str, int] = {}
        self._symbol_ids: list[str] = []

    # ─── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        if not config.feeds.binance.enabled:
            return

        bus.subscribe(config.protocol.topics.get("cmd_pause_feed", "cmd_pause_feed"), self._on_pause)
        bus.subscribe(config.protocol.topics.get("cmd_resume_feed", "cmd_resume_feed"), self._on_resume)

        self._symbol_ids = [s.id for s in registry.enabled() if s.source == "binance" and s.binance]
        if not self._symbol_ids:
            log.warning("Binance feed enabled but no symbols have a "
                        "`binance` mapping. Nothing to stream.")
            return

        self._running = True
        self._http = aiohttp.ClientSession()

        for sym_id in self._symbol_ids:
            self._books[sym_id] = self._new_book_state()
            self._buffers[sym_id] = []
            self._depth_count[sym_id] = 0
            self._tasks.append(asyncio.create_task(self._symbol_loop(sym_id)))
            log.info("Binance feed starting worker for %s", sym_id)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

        if self._http is not None:
            await self._http.close()
            self._http = None

    async def _on_pause(self, _topic: str, payload: dict) -> None:
        self._paused = True
        log.info("Binance feed: publishing PAUSED (test isolation)")

    async def _on_resume(self, _topic: str, payload: dict) -> None:
        self._paused = False
        log.info("Binance feed: publishing RESUMED")

    @staticmethod
    def _new_book_state() -> dict[str, Any]:
        return {
            "bids": {},
            "asks": {},
            "lastUpdateId": 0,
            "ready": False,
            "first_event_accepted": False,
        }

    # ─── Per-symbol driver ──────────────────────────────────

    async def _symbol_loop(self, symbol_id: str) -> None:
        backoff = config.feeds.binance.reconnect.initial_ms / 1000
        max_backoff = config.feeds.binance.reconnect.max_ms / 1000
        multiplier = config.feeds.binance.reconnect.multiplier

        while self._running:
            try:
                await self._run_symbol(symbol_id)
                backoff = config.feeds.binance.reconnect.initial_ms / 1000
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning(
                    "Binance feed [%s] error: %s — reconnecting in %.1fs",
                    symbol_id, exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * multiplier, max_backoff)

    async def _run_symbol(self, symbol_id: str) -> None:
        sym = registry.require(symbol_id)
        binance_symbol = sym.binance
        if not binance_symbol:
            return

        lower = binance_symbol.lower()
        speed = config.feeds.binance.depth_stream_speed_ms
        depth_stream = f"{lower}@depth@{speed}ms"
        trade_stream = f"{lower}@aggTrade"

        base = config.feeds.binance.ws_base.rstrip("/")
        url = f"{base}/stream?streams={depth_stream}/{trade_stream}"

        log.info("Binance feed [%s] connecting: %s", symbol_id, url)

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=2 ** 22,
        ) as ws:
            self._buffers[symbol_id] = []
            self._books[symbol_id] = self._new_book_state()

            snapshot_task = asyncio.create_task(
                self._fetch_snapshot(symbol_id, binance_symbol)
            )

            try:
                async for raw in ws:
                    if not self._running:
                        break
                    self._on_message(symbol_id, raw)
            finally:
                if not snapshot_task.done():
                    snapshot_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await snapshot_task

    # ─── Snapshot ───────────────────────────────────────────

    async def _fetch_snapshot(self, symbol_id: str, binance_symbol: str) -> None:
        assert self._http is not None
        rest = config.feeds.binance.rest_base.rstrip("/")
        limit = config.feeds.binance.depth_snapshot_limit
        url = f"{rest}/api/v3/depth?symbol={binance_symbol}&limit={limit}"

        try:
            async with self._http.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:
            log.warning("Binance feed [%s] snapshot failed: %s", symbol_id, exc)
            return

        book = self._books[symbol_id]
        book["bids"] = {float(p): float(q) for p, q in data["bids"]}
        book["asks"] = {float(p): float(q) for p, q in data["asks"]}
        book["lastUpdateId"] = int(data["lastUpdateId"])
        book["ready"] = False
        book["first_event_accepted"] = False

        buffered = self._buffers[symbol_id]
        self._buffers[symbol_id] = []

        log.info(
            "Binance feed [%s] snapshot received: lastUpdateId=%d  "
            "bids=%d asks=%d  buffered=%d",
            symbol_id, book["lastUpdateId"],
            len(book["bids"]), len(book["asks"]),
            len(buffered),
        )

        for evt in buffered:
            self._apply_depth(symbol_id, evt)

    # ─── Message dispatch ───────────────────────────────────

    def _on_message(self, symbol_id: str, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        stream = msg.get("stream", "")
        data = msg.get("data", msg)

        if "@depth" in stream:
            self._handle_depth(symbol_id, data)
        elif "@aggTrade" in stream:
            self._handle_trade(symbol_id, data)

    # ─── Depth handling ─────────────────────────────────────

    def _handle_depth(self, symbol_id: str, event: dict) -> None:
        book = self._books.get(symbol_id)
        if book is None:
            return

        if book["lastUpdateId"] == 0:
            self._buffers[symbol_id].append(event)
            return

        self._apply_depth(symbol_id, event)

    def _apply_depth(self, symbol_id: str, event: dict) -> None:
        book = self._books[symbol_id]
        U = int(event["U"])
        u = int(event["u"])

        if not book["first_event_accepted"]:
            if u <= book["lastUpdateId"]:
                return
            book["first_event_accepted"] = True
            book["ready"] = True
            book["lastUpdateId"] = u
            log.info(
                "Binance feed [%s] first event accepted: U=%d u=%d "
                "(snapshot was lastUpdateId=%d, gap=%d)",
                symbol_id, U, u, book["lastUpdateId"], U - book["lastUpdateId"],
            )
        else:
            if U > book["lastUpdateId"] + 1:
                log.warning(
                    "Binance feed [%s] depth gap: expected U=%d, got U=%d — resyncing",
                    symbol_id, book["lastUpdateId"] + 1, U,
                )
                book["ready"] = False
                book["lastUpdateId"] = 0
                book["first_event_accepted"] = False
                self._buffers[symbol_id] = [event]
                sym = registry.require(symbol_id)
                if sym.binance:
                    asyncio.create_task(
                        self._fetch_snapshot(symbol_id, sym.binance)
                    )
                return
            book["lastUpdateId"] = u

        for price_s, qty_s in event.get("b", []):
            price = float(price_s)
            qty = float(qty_s)
            if qty == 0.0:
                book["bids"].pop(price, None)
            else:
                book["bids"][price] = qty

        for price_s, qty_s in event.get("a", []):
            price = float(price_s)
            qty = float(qty_s)
            if qty == 0.0:
                book["asks"].pop(price, None)
            else:
                book["asks"][price] = qty

        self._publish_book(symbol_id)

    # ─── Trade handling ─────────────────────────────────────

    def _handle_trade(self, symbol_id: str, event: dict) -> None:
        if self._paused:
            return
        try:
            price = float(event["p"])
            qty = float(event["q"])
            is_buyer_maker = bool(event["m"])
            ts = int(event["T"])
        except (KeyError, TypeError, ValueError):
            return

        side = "SELL" if is_buyer_maker else "BUY"

        asyncio.create_task(bus.publish(
            config.protocol.topics["feed_trade"],
            {
                "symbol": symbol_id,
                "trade": {
                    "ts": ts,
                    "price": price,
                    "qty": qty,
                    "side": side,
                },
            },
        ))

    # ─── Publish book ───────────────────────────────────────

    def _publish_book(self, symbol_id: str) -> None:
        if self._paused:
            return
        book = self._books[symbol_id]
        if not book["ready"]:
            return

        self._depth_count[symbol_id] += 1
        if self._depth_count[symbol_id] % 100 == 0:
            best_bid = max(book["bids"].keys()) if book["bids"] else None
            best_ask = min(book["asks"].keys()) if book["asks"] else None
            log.info(
                "Binance feed [%s] depth #%d  best_bid=%s  best_ask=%s  levels=%d/%d",
                symbol_id, self._depth_count[symbol_id],
                best_bid, best_ask,
                len(book["bids"]), len(book["asks"]),
            )

        keep = config.book.levels_to_keep
        bids = sorted(book["bids"].items(), key=lambda kv: -kv[0])[:keep]
        asks = sorted(book["asks"].items(), key=lambda kv: kv[0])[:keep]

        asyncio.create_task(bus.publish(
            config.protocol.topics["feed_depth"],
            {
                "symbol": symbol_id,
                "book": {
                    "bids": [[p, q] for p, q in bids],
                    "asks": [[p, q] for p, q in asks],
                },
            },
        ))