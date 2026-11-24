from __future__ import annotations

import asyncio
import logging
import time

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry

log = logging.getLogger(__name__)


class TapeAggregator(BaseModule):
    """Batches `feed.trade` events into `tape.append` messages.

    Only the currently-active symbol's trades are buffered and published,
    so the tape widget never displays cross-symbol noise.
    """

    name = "aggregator.tape_aggregator"

    def __init__(self) -> None:
        self._buffer: dict[str, list] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        bus.subscribe(config.protocol.topics["feed_trade"], self._on_trade)
        self._task = asyncio.create_task(self._publish_loop())
        log.info("TapeAggregator started (%.1f Hz)", config.tape.publish_hz)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _on_trade(self, _topic: str, payload: dict) -> None:
        # ─── ACTIVE-SYMBOL GATE ───
        if registry.get_active() != payload.get("symbol"):
            return
        sym = payload["symbol"]
        buf = self._buffer.setdefault(sym, [])
        buf.append(payload["trade"])
        if len(buf) > config.tape.buffer_size:
            del buf[: len(buf) - config.tape.buffer_size]

    async def _publish_loop(self) -> None:
        interval = 1.0 / max(config.tape.publish_hz, 1)
        topic = config.protocol.topics["tape_append"]
        while True:
            active = registry.get_active()
            if active and active in self._buffer and self._buffer[active]:
                trades = self._buffer[active]
                await bus.publish(topic, {
                    "symbol": active,
                    "ts": int(time.time() * 1000),
                    "data": {"trades": trades[-30:]},
                })
                self._buffer[active] = []
            await asyncio.sleep(interval)