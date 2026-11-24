"""cTrader Open API feed for XAUUSD.

Uses ctrader-api-client (async, no Twisted).
Connects to LIVE server (BlackBull Markets) using the real cTID account ID.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry

log = logging.getLogger(__name__)

try:
    from ctrader_api_client import (
        CTraderClient, ClientConfig, AccountCredentials,
        ReadyEvent, SpotEvent, DepthEvent,
    )
    CTRADER_AVAILABLE = True
except ImportError:
    CTRADER_AVAILABLE = False
    log.warning("ctrader-api-client not installed. Run: pip install ctrader-api-client")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _build_client_config() -> ClientConfig:
    """Build ClientConfig with explicit host/port for live/demo environment."""
    client_id = os.getenv("CTRADER_CLIENT_ID", "").strip()
    client_secret = os.getenv("CTRADER_CLIENT_SECRET", "").strip()

    if config.feeds.ctrader.environment == "live":
        host = "live.ctraderapi.com"
    else:
        host = "demo.ctraderapi.com"

    log.info(
        f"CTraderFeed: Using host={host} port=5035 "
        f"(environment={config.feeds.ctrader.environment})"
    )

    return ClientConfig(
        client_id=client_id,
        client_secret=client_secret,
        host=host,
        port=5035,
    )


class CTraderFeed(BaseModule):
    name = "feed.ctrader"

    def __init__(self) -> None:
        self._running = False
        self._client: Any = None
        self._client_task: asyncio.Task | None = None
        self._account_id: int = 0
        self._xau_symbol_id: int = 0
        self._xau_symbol_id_canonical: str = "XAUUSD"

        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}

        self._last_bid: float = 0.0
        self._last_ask: float = 0.0
        self._last_tick_ms: int = 0

        self._bar_open_ts: int = 0
        self._bar_o = self._bar_h = self._bar_l = self._bar_c = 0.0
        self._bar_vol = 0.0

    # ─── Lifecycle ─────────────────────────────────────────

    async def start(self) -> None:
        if not CTRADER_AVAILABLE:
            log.error("CTraderFeed: ctrader-api-client package missing")
            return
        if not config.feeds.ctrader.enabled:
            log.info("CTraderFeed: disabled in config")
            return
        if getattr(config, "synthetic", None) and config.synthetic.enabled:
            log.info("CTraderFeed suppressed: synthetic mode is active")
            return

        self._account_id = config.feeds.ctrader.account_id
        self._xau_symbol_id = config.feeds.ctrader.symbol_id_xauusd

        access_token = os.getenv("CTRADER_ACCESS_TOKEN", "").strip()
        refresh_token = os.getenv("CTRADER_REFRESH_TOKEN", "").strip()
        expires_at = int(os.getenv("CTRADER_TOKEN_EXPIRY", "0") or "0")

        log.info("CTraderFeed: ─── AUTH DIAGNOSTICS ───")
        log.info(f"CTraderFeed: account_id   = {self._account_id}")
        log.info(f"CTraderFeed: symbol_id    = {self._xau_symbol_id}")
        log.info(f"CTraderFeed: expires_at   = {expires_at} "
                 f"(now={int(time.time())}, delta={expires_at - int(time.time())}s)")
        log.info(f"CTraderFeed: environment  = {config.feeds.ctrader.environment}")
        log.info("CTraderFeed: ─────────────────────────")

        if expires_at <= int(time.time()):
            log.warning("CTraderFeed: access token is EXPIRED — will attempt refresh")
        if not access_token or not refresh_token:
            log.error("CTraderFeed: tokens missing from .env")
            return
        if self._account_id <= 0:
            log.error("CTraderFeed: account_id must be > 0")
            return

        try:
            cfg = _build_client_config()
        except Exception as exc:
            log.exception(f"CTraderFeed: ClientConfig build failed: {exc}")
            return

        self._client = CTraderClient(cfg)

        @self._client.on(SpotEvent, symbol_id=self._xau_symbol_id)
        async def _on_spot(event: SpotEvent) -> None:
            await self._handle_spot(event)

        @self._client.on(DepthEvent, symbol_id=self._xau_symbol_id)
        async def _on_depth(event: DepthEvent) -> None:
            await self._handle_depth(event)

        @self._client.on(ReadyEvent)
        async def _on_ready(event: ReadyEvent) -> None:
            log.info("cTrader ready — subscribing to XAUUSD streams")

        self._running = True
        self._client_task = asyncio.create_task(self._run_client_with_retry(
            access_token, refresh_token, expires_at
        ))
        log.info(f"CTraderFeed started for XAUUSD (symbol_id={self._xau_symbol_id})")

    async def stop(self) -> None:
        self._running = False
        if self._client_task:
            self._client_task.cancel()
            try:
                await self._client_task
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
        log.info("CTraderFeed stopped")

    # ─── Manual retry with backoff ─────────────────────────

    async def _run_client_with_retry(
        self, access_token: str, refresh_token: str, expires_at: int
    ) -> None:
        max_attempts = 3
        backoff = 5.0

        for attempt in range(1, max_attempts + 1):
            try:
                await self._run_client_once(access_token, refresh_token, expires_at)
                log.info("CTraderFeed: client exited cleanly")
                return
            except asyncio.CancelledError:
                raise
            except OverflowError as exc:
                log.error(f"CTraderFeed: qasync overflow (anyio inf sleep): {exc}")
                return
            except Exception as exc:
                log.error(f"CTraderFeed: attempt {attempt}/{max_attempts} failed: "
                          f"{type(exc).__name__}: {exc}")
                if attempt < max_attempts and self._running:
                    log.info(f"CTraderFeed: retrying in {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                else:
                    log.error("CTraderFeed: giving up after max attempts")
                    return

    async def _run_client_once(
        self, access_token: str, refresh_token: str, expires_at: int
    ) -> None:
        async with self._client:
            try:
                log.info("CTraderFeed: authenticating trader...")
                await self._client.auth.authenticate_trader(
                    AccountCredentials(
                        account_id=self._account_id,
                        access_token=access_token,
                        refresh_token=refresh_token,
                        expires_at=expires_at,
                    )
                )
                log.info("cTrader account authenticated ✅")
            except Exception as exc:
                log.error(f"CTraderFeed: authenticate_trader FAILED: "
                          f"{type(exc).__name__}: {exc}")
                raise

            try:
                await self._client.market_data.subscribe_depth(
                    self._account_id, [self._xau_symbol_id]
                )
                log.info("cTrader: subscribed to depth")
            except Exception as exc:
                log.error(f"CTraderFeed: depth subscribe failed: {exc}")
                raise

            try:
                await self._client.market_data.subscribe_spots(
                    self._account_id, [self._xau_symbol_id]
                )
                log.info("cTrader: subscribed to spots")
            except Exception as exc:
                log.error(f"CTraderFeed: spots subscribe failed: {exc}")
                raise

            log.info("cTrader subscriptions active: depth + spots")

            while self._running:
                await asyncio.sleep(1.0)

    # ─── Handlers ──────────────────────────────────────────

    async def _handle_spot(self, event: SpotEvent) -> None:
        try:
            bid = float(event.bid) / 100000.0
            ask = float(event.ask) / 100000.0
        except (TypeError, ValueError, AttributeError):
            return
        if bid <= 0 or ask <= 0 or not self._is_active():
            return

        now = _now_ms()
        if config.feeds.ctrader.infer_trade_side:
            side = self._infer_trade(bid, ask)
            if side:
                await bus.publish(
                    config.protocol.topics["feed_trade"],
                    {
                        "symbol": self._xau_symbol_id_canonical,
                        "trade": {
                            "ts": now,
                            "price": round(bid if side == "SELL" else ask, 2),
                            "qty": 1.0,
                            "side": side,
                        },
                    },
                )

        self._update_live_bar(now, bid, ask)
        self._last_bid = bid
        self._last_ask = ask
        self._last_tick_ms = now

    def _infer_trade(self, bid: float, ask: float) -> str | None:
        if self._last_bid <= 0:
            return None
        if bid > self._last_bid:
            return "BUY"
        if ask < self._last_ask:
            return "SELL"
        return None

    async def _handle_depth(self, event: DepthEvent) -> None:
        if not self._is_active():
            return
        try:
            for q in getattr(event, "new_quotes", []):
                size = float(q.size) / 100.0
                bid = float(q.bid) / 100000.0 if q.bid else 0.0
                ask = float(q.ask) / 100000.0 if q.ask else 0.0
                if bid > 0:
                    if size <= 0:
                        self._bids.pop(bid, None)
                    else:
                        self._bids[bid] = size
                if ask > 0:
                    if size <= 0:
                        self._asks.pop(ask, None)
                    else:
                        self._asks[ask] = size
        except (AttributeError, TypeError, ValueError):
            return

        bids = sorted(self._bids.items(), key=lambda kv: -kv[0])[:50]
        asks = sorted(self._asks.items(), key=lambda kv: kv[0])[:50]
        await bus.publish(
            config.protocol.topics["feed_depth"],
            {
                "symbol": self._xau_symbol_id_canonical,
                "book": {
                    "bids": [[p, q] for p, q in bids],
                    "asks": [[p, q] for p, q in asks],
                },
            },
        )

    def _update_live_bar(self, now_ms: int, bid: float, ask: float) -> None:
        mid = (bid + ask) / 2.0
        bar_start = (now_ms // 60000) * 60000

        if bar_start != self._bar_open_ts:
            if self._bar_open_ts > 0 and self._bar_o > 0:
                asyncio.create_task(bus.publish(
                    config.protocol.topics["feed_kline"],
                    {
                        "symbol": self._xau_symbol_id_canonical,
                        "interval": "1m",
                        "kline": {
                            "t": self._bar_open_ts, "o": self._bar_o,
                            "h": self._bar_h, "l": self._bar_l,
                            "c": self._bar_c, "v": self._bar_vol,
                            "closed": True,
                        },
                    },
                ))
            self._bar_open_ts = bar_start
            self._bar_o = self._bar_h = self._bar_l = self._bar_c = mid
            self._bar_vol = 0.0
        else:
            self._bar_h = max(self._bar_h, mid)
            self._bar_l = min(self._bar_l, mid)
            self._bar_c = mid
            self._bar_vol += 1.0

    def _is_active(self) -> bool:
        return registry.get_active() == self._xau_symbol_id_canonical