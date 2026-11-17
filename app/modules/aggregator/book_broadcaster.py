from __future__ import annotations

import asyncio
import logging
import time

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry

log = logging.getLogger(__name__)


class BookBroadcaster(BaseModule):
    """Throttles `feed.depth` events into `book.update` at a fixed rate.

    Only the currently-active symbol is published downstream, so widgets
    (orderbook, depth chart, heatmap) never receive cross-symbol noise.
    """

    name = "aggregator.book_broadcaster"

    def __init__(self) -> None:
        self._latest: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._count: int = 0

    async def start(self) -> None:
        bus.subscribe(config.protocol.topics["feed_depth"], self._on_depth)
        self._task = asyncio.create_task(self._publish_loop())
        log.info("BookBroadcaster started (%.1f Hz)", config.book.publish_hz)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _on_depth(self, _topic: str, payload: dict) -> None:
        self._latest[payload["symbol"]] = payload["book"]

    async def _publish_loop(self) -> None:
        interval = 1.0 / max(config.book.publish_hz, 1)
        topic = config.protocol.topics["book_update"]
        while True:
            # ─── ACTIVE-SYMBOL GATE ───
            active = registry.get_active()
            if active and active in self._latest:
                book = self._latest[active]
                await bus.publish(topic, {
                    "symbol": active,
                    "ts": int(time.time() * 1000),
                    "data": {"bids": book["bids"], "asks": book["asks"]},
                })
                self._count += 1
                if self._count % 50 == 0:
                    log.info(
                        "BookBroadcaster published #%d for %s (bids=%d asks=%d)",
                        self._count, active,
                        len(book["bids"]), len(book["asks"]),
                    )
            await asyncio.sleep(interval)