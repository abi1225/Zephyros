from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import aiohttp
import websockets

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry

log = logging.getLogger(__name__)


class KlineFeed(BaseModule):
    """Pure live kline stream. Warm-up history is fetched by the
    strategy manager on START, not by this module.

    Supports pause/resume for isolated pipeline testing.
    """

    name = "feed.klines"

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._paused = False
        self._http: aiohttp.ClientSession | None = None
        self._symbols: list[str] = []

    async def start(self) -> None:
        if not config.feeds.klines.enabled:
            return

        bus.subscribe(config.protocol.topics.get("cmd_pause_feed", "cmd_pause_feed"), self._on_pause)
        bus.subscribe(config.protocol.topics.get("cmd_resume_feed", "cmd_resume_feed"), self._on_resume)

        self._symbols = [s.id for s in registry.enabled() if s.source == "binance" and s.binance]
        if not self._symbols:
            log.warning(
                "Kline feed enabled but no symbols have a binance mapping",
            )
            return

        self._running = True
        self._http = aiohttp.ClientSession()

        intervals = self._intervals()
        log.info(
            "Kline feed starting: %d symbol(s), intervals=%s",
            len(self._symbols), intervals,
        )

        self._tasks.append(asyncio.create_task(self._stream_loop(intervals)))

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
        log.info("Kline feed: publishing PAUSED (test isolation)")

    async def _on_resume(self, _topic: str, payload: dict) -> None:
        self._paused = False
        log.info("Kline feed: publishing RESUMED")

    def _intervals(self) -> list[str]:
        candle_cfg = getattr(config.ui, "candlestick", None) or {}
        ivs = candle_cfg.get("available_intervals")
        if ivs:
            return [str(i) for i in ivs]
        return [str(config.feeds.klines.interval)]

    # ─── Live stream ────────────────────────────────────────

    async def _stream_loop(self, intervals: list[str]) -> None:
        backoff = config.feeds.klines.reconnect.initial_ms / 1000
        max_backoff = config.feeds.klines.reconnect.max_ms / 1000
        multiplier = config.feeds.klines.reconnect.multiplier

        while self._running:
            try:
                await self._run_stream(intervals)
                backoff = config.feeds.klines.reconnect.initial_ms / 1000
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning(
                    "Kline stream error: %s — reconnect in %.1fs",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * multiplier, max_backoff)

    async def _run_stream(self, intervals: list[str]) -> None:
        base = config.feeds.binance.ws_base.rstrip("/")

        streams = []
        for sym_id in self._symbols:
            sym = registry.require(sym_id)
            if not sym.binance:
                continue
            for iv in intervals:
                streams.append(f"{sym.binance.lower()}@kline_{iv}")

        if not streams:
            return

        url = f"{base}/stream?streams={'/'.join(streams)}"
        log.info("Kline stream connecting: %d stream(s)", len(streams))

        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, close_timeout=5,
            max_size=2 ** 22,
        ) as ws:
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                data = msg.get("data", {})
                k = data.get("k")
                if not k:
                    continue

                binance_symbol = k.get("s", "")
                interval = k.get("i", "")
                sym = registry.by_binance(binance_symbol)
                if sym is None or not interval:
                    continue

                if self._paused:
                    continue

                await bus.publish(
                    config.protocol.topics["feed_kline"],
                    {
                        "symbol": sym.id,
                        "interval": interval,
                        "kline": {
                            "t": int(k["t"]),
                            "o": float(k["o"]),
                            "h": float(k["h"]),
                            "l": float(k["l"]),
                            "c": float(k["c"]),
                            "v": float(k["v"]),
                            "closed": bool(k.get("x", False)),
                        },
                    },
                )