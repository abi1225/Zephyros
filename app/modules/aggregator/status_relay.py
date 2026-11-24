from __future__ import annotations

import asyncio
import time

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule


class StatusRelay(BaseModule):
    """Publishes a heartbeat on `status_update` at 1 Hz.

    Tracks two independent inputs:
      - feed liveness, from `feed.depth` timestamps
      - MT5 connection state, from `mt5.status` events

    Both are merged into one status payload for the UI ribbon and
    status bar.
    """

    name = "aggregator.status_relay"

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._last_feed_ts: int = 0
        self._mt5_connected: bool = False

    async def start(self) -> None:
        bus.subscribe(config.protocol.topics["feed_depth"], self._on_feed)
        bus.subscribe(config.protocol.topics["mt5_status"], self._on_mt5_status)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ─── Inputs ─────────────────────────────────────────────

    async def _on_feed(self, _topic: str, payload: dict) -> None:
        self._last_feed_ts = int(time.time() * 1000)

    async def _on_mt5_status(self, _topic: str, payload: dict) -> None:
        self._mt5_connected = bool(payload.get("connected", False))

    # ─── Heartbeat ──────────────────────────────────────────

    async def _loop(self) -> None:
        topic = config.protocol.topics["status_update"]
        while True:
            now = int(time.time() * 1000)
            latency = (now - self._last_feed_ts) if self._last_feed_ts else 0
            connected = self._last_feed_ts > 0 and latency < 5000
            await bus.publish(topic, {
                "connected": connected,
                "feed_latency_ms": latency,
                "mt5_connected": self._mt5_connected,
            })
            await asyncio.sleep(1.0)