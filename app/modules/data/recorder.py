from __future__ import annotations

import asyncio
import logging
import time

from app.core.config import config
from app.core.data_store import get_l2_store
from app.core.event_bus import bus
from app.core.module import BaseModule

log = logging.getLogger(__name__)


class L2Recorder(BaseModule):
    name = "data.l2_recorder"

    def __init__(self) -> None:
        self._running = False
        self._store = get_l2_store()
        self._depth_topic: str | None = None
        self._trade_topic: str | None = None
        # Per-symbol throttle so we don't write 10 snapshots/sec.
        self._snapshot_throttle_ms: int = 1000
        self._last_snapshot_ts: dict[str, int] = {}

    async def start(self) -> None:
        mode = config.data_pipeline.storage_mode

        if mode != "local":
            log.info(
                f"L2 recorder idle — storage_mode={mode!r}. "
                f"VPS handles its own recording."
            )
            return

        if self._store is None:
            return

        self._snapshot_throttle_ms = int(
            getattr(
                config.data_pipeline.local,
                "snapshot_throttle_ms",
                1000,
            )
        )

        self._depth_topic = config.protocol.topics["feed_depth"]
        self._trade_topic = config.protocol.topics["feed_trade"]
        bus.subscribe(self._depth_topic, self._on_depth)
        bus.subscribe(self._trade_topic, self._on_trade)
        self._running = True
        log.info(
            "L2 recorder active — writing snapshots + trades to local "
            "SQLite (snapshot throttle=%dms)",
            self._snapshot_throttle_ms,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._depth_topic:
            bus.unsubscribe(self._depth_topic, self._on_depth)
        if self._trade_topic:
            bus.unsubscribe(self._trade_topic, self._on_trade)
        log.info("L2 recorder stopped")

    async def _on_depth(self, _topic: str, payload: dict) -> None:
        if not self._running or not self._store:
            return
        symbol = payload.get("symbol")
        book = payload.get("book")
        if not symbol or not book:
            return

        # Throttle per-symbol snapshot writes.
        now_ms = int(time.time() * 1000)
        last = self._last_snapshot_ts.get(symbol, 0)
        if now_ms - last < self._snapshot_throttle_ms:
            return
        self._last_snapshot_ts[symbol] = now_ms

        asyncio.create_task(
            self._store.save_snapshot(
                timestamp=now_ms,
                symbol=symbol,
                bids=book.get("bids", []),
                asks=book.get("asks", []),
            )
        )

    async def _on_trade(self, _topic: str, payload: dict) -> None:
        if not self._running or not self._store:
            return
        symbol = payload.get("symbol")
        trade = payload.get("trade")
        if not symbol or not trade:
            return
        try:
            ts = int(trade.get("ts", time.time() * 1000))
            price = float(trade["price"])
            qty = float(trade["qty"])
            side = str(trade["side"])
        except (KeyError, TypeError, ValueError):
            return
        asyncio.create_task(
            self._store.save_trade(ts, symbol, price, qty, side)
        )