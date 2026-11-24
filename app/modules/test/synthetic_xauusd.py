from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


class SyntheticXAUUSD(BaseModule):
    name = "test.synthetic_xauusd"

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self.symbol = config.synthetic.symbol
        self.base_price = config.synthetic.base_price
        self._phase = "WARMUP"
        self._phase_start = 0
        self._current_price = self.base_price

    async def start(self) -> None:
        if not config.synthetic.enabled:
            return
        self._running = True
        log.info(f"Synthetic XAUUSD Generator started for {self.symbol} @ {self.base_price}")
        await self._inject_history()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Synthetic XAUUSD Generator stopped")

    async def _inject_history(self) -> None:
        now = _now_ms()
        base_t = now - (50 * 60 * 1000)
        for i in range(50):
            t = base_t + (i * 60 * 1000)
            o = self.base_price + random.uniform(-1.0, 1.0)
            c = o + random.uniform(-0.5, 0.5)
            h = max(o, c) + random.uniform(0.1, 0.5)
            l = min(o, c) - random.uniform(0.1, 0.5)
            if i > 45:
                o = self.base_price - 0.05
                c = self.base_price
                h = self.base_price + 0.10
                l = self.base_price - 0.10
            await bus.publish(config.protocol.topics["feed_kline"], {
                "symbol": self.symbol,
                "interval": "1m",
                "kline": {
                    "t": t, "o": o, "h": h, "l": l, "c": c,
                    "v": 1000.0, "closed": True,
                }
            })
            await asyncio.sleep(0.01)

    async def _run_loop(self) -> None:
        try:
            while self._running:
                now = _now_ms()
                if self._phase_start == 0:
                    self._phase_start = now
                elapsed = (now - self._phase_start) / 1000.0

                # Wall placement: ask wall at base_price + 6.00 (satisfies next_wall check)
                wall_ask = self.base_price + 6.00

                if self._phase == "WARMUP":
                    await self._publish_depth(
                        self.base_price - 0.01, self.base_price + 0.01,
                        wall_bid=50.0, wall_ask_price=wall_ask,
                    )
                    await self._publish_trade(self.base_price, 0.5, "BUY")
                    if elapsed > 3.0:
                        self._advance_phase("APPROACH")

                elif self._phase == "APPROACH":
                    # Drift down to the level
                    self._current_price = max(self.base_price, self._current_price - 0.05)
                    await self._publish_depth(
                        self._current_price - 0.01, self._current_price + 0.01,
                        wall_bid=50.0, wall_ask_price=wall_ask,
                    )
                    await self._publish_trade(self._current_price, 0.5, "SELL")
                    if self._current_price <= self.base_price + 0.01:
                        self._advance_phase("TEST_SELLERS")

                elif self._phase == "TEST_SELLERS":
                    # Aggressive sellers hit the level. Price stays exactly at level.
                    await self._publish_depth(
                        self.base_price, self.base_price + 0.01,
                        wall_bid=50.0, wall_ask_price=wall_ask,
                    )
                    for _ in range(15):
                        await self._publish_trade(
                            self.base_price, random.uniform(1.0, 3.0), "SELL"
                        )
                        await asyncio.sleep(0.05)
                    if elapsed > 3.0:
                        self._advance_phase("FLIP_BUYERS")

                elif self._phase == "FLIP_BUYERS":
                    # Buyers flip the tape. Price = level (within tolerance).
                    await self._publish_depth(
                        self.base_price - 0.02, self.base_price + 0.02,
                        wall_bid=50.0, wall_ask_price=wall_ask,
                    )
                    for _ in range(20):
                        await self._publish_trade(
                            self.base_price, random.uniform(2.0, 4.0), "BUY"
                        )
                        await asyncio.sleep(0.05)
                    if elapsed > 2.0:
                        self._advance_phase("SPIKE")

                elif self._phase == "SPIKE":
                    # Massive tape spike for the entry trigger
                    await self._publish_depth(
                        self.base_price + 0.02, self.base_price + 0.05,
                        wall_ask_price=wall_ask,
                    )
                    for _ in range(30):
                        await self._publish_trade(
                            self.base_price + 0.05, random.uniform(5.0, 10.0), "BUY"
                        )
                        await asyncio.sleep(0.02)
                    if elapsed > 2.0:
                        self._advance_phase("RUN_UP")

                elif self._phase == "RUN_UP":
                    self._current_price += 0.10
                    await self._publish_depth(
                        self._current_price - 0.05, self._current_price + 0.05,
                        wall_ask_price=wall_ask,
                    )
                    await self._publish_trade(self._current_price, 1.0, "BUY")
                    if self._current_price >= self.base_price + 6.00:
                        self._advance_phase("RESET")

                elif self._phase == "RESET":
                    log.info("Synthetic: Resetting script to WARMUP")
                    self._current_price = self.base_price
                    self._advance_phase("WARMUP")

                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception(f"Synthetic loop error: {exc}")

    def _advance_phase(self, new_phase: str) -> None:
        log.info(f"Synthetic XAUUSD Phase: {self._phase} → {new_phase}")
        self._phase = new_phase
        self._phase_start = _now_ms()

    async def _publish_depth(
        self,
        bid: float,
        ask: float,
        wall_bid: float = 0.0,
        wall_ask_price: float = 0.0,
    ) -> None:
        if registry.get_active() != self.symbol:
            return

        bids = [[round(bid - i * 0.01, 2), round(random.uniform(0.5, 2.0), 2)] for i in range(20)]
        asks = [[round(ask + i * 0.01, 2), round(random.uniform(0.5, 2.0), 2)] for i in range(20)]

        if wall_bid > 0:
            bids[0][1] = wall_bid

        if wall_ask_price > 0:
            # Inject a large ask wall at the target price
            # Ensure the ask list includes that price level
            asks = [[p, q] for p, q in asks if p != wall_ask_price]
            asks.append([round(wall_ask_price, 2), 50.0])
            asks.sort(key=lambda x: x[0])

        await bus.publish(config.protocol.topics["feed_depth"], {
            "symbol": self.symbol,
            "book": {"bids": bids, "asks": asks},
        })

    async def _publish_trade(self, price: float, qty: float, side: str) -> None:
        if registry.get_active() != self.symbol:
            return

        await bus.publish(config.protocol.topics["feed_trade"], {
            "symbol": self.symbol,
            "trade": {
                "ts": _now_ms(),
                "price": round(price, 2),
                "qty": round(qty, 2),
                "side": side,
            },
        })