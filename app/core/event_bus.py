from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

Handler = Callable[[str, Any], Awaitable[None]]

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        # Strong references to in-flight tasks. Without this,
        # asyncio may garbage-collect a task before it runs.
        self._tasks: set[asyncio.Task] = set()

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        if handler in self._subs.get(topic, []):
            self._subs[topic].remove(handler)

    async def publish(self, topic: str, payload: Any) -> None:
        # Snapshot so handlers that (un)subscribe mid-flight don't
        # mutate the list we're iterating.
        for handler in list(self._subs.get(topic, [])):
            task = asyncio.create_task(
                self._dispatch(handler, topic, payload)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _dispatch(
        self, handler: Handler, topic: str, payload: Any
    ) -> None:
        try:
            await handler(topic, payload)
        except asyncio.CancelledError:
            # Propagate cancellation so awaiting stop() doesn't hang.
            raise
        except Exception:
            log.exception(
                "Event handler %r failed on topic %r",
                getattr(handler, "__qualname__", handler),
                topic,
            )


bus = EventBus()