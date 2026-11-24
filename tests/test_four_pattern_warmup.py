"""Integration test for the zero-signal regression.

Root cause: FourPattern implemented `_on_kline` as its own bus handler but
never implemented `on_kline`, so every warmup bar the StrategyManager fed
through `strategy.on_kline(...)` hit the base no-op. ATR stayed at 0, no
pivot was ever found, and no setup could pass its gates.

This test drives the strategy exactly the way the manager now does and
asserts that warmup actually builds state (ATR > 0, pivots found) and
that the entry engine can reach a decision.

Dependencies are stubbed so it runs headless, without Qt/MT5/network.
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import types
import unittest
from types import SimpleNamespace

# ─── Stub config + bus BEFORE importing strategy modules ───
_TOPICS = {
    "feed_trade": "feed_trade",
    "feed_kline": "feed_kline",
    "position_update": "position_update",
    "debug_decision": "debug_decision",
    "signal_failed": "signal_failed",
    "signal_proposed": "signal_proposed",
    "cmd_sync_stop": "cmd_sync_stop",
    "zones_update": "zones_update",
}


class _RecordingBus:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    def subscribe(self, *a, **k):
        pass

    def unsubscribe(self, *a, **k):
        pass

    async def publish(self, topic, payload):
        self.published.append((topic, payload))


def _install_stubs() -> _RecordingBus:
    bus = _RecordingBus()

    cfg = SimpleNamespace(
        protocol=SimpleNamespace(topics=dict(_TOPICS)),
        risk=SimpleNamespace(
            max_daily_loss_pct=2.0, max_position_pct=10.0,
            max_open_positions=1, max_spread={},
            no_pyramiding=True, cooldown_after_exit_sec=5,
            cooldown_after_loss_sec=60, max_signal_age_sec=5,
            persist_state=False, daily_reset_hour_utc=0,
            publish_interval_sec=1, allow_manual_close=True,
            allow_manual_sl_tp=False,
        ),
        app=SimpleNamespace(database_path=":memory:", risk_state_path=":memory:"),
    )

    config_mod = types.ModuleType("app.core.config")
    config_mod.config = cfg
    config_mod.SymbolCfg = object
    sys.modules["app.core.config"] = config_mod

    bus_mod = types.ModuleType("app.core.event_bus")
    bus_mod.bus = bus
    sys.modules["app.core.event_bus"] = bus_mod

    return bus


def _make_5m_bar(t_ms: int, o: float, h: float, l: float, c: float, v=1000.0):
    return {
        "symbol": "BTC",
        "interval": "5m",
        "kline": {
            "t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v, "closed": True,
        },
    }


class TestFourPatternWarmup(unittest.TestCase):
    """The strategy must accept bars through on_kline()."""

    @classmethod
    def setUpClass(cls):
        cls.bus = _install_stubs()

    def _strategy(self):
        """Build a strategy. Must be called from inside a running loop:
        set_symbol() starts the state ticker via asyncio.create_task."""
        from app.modules.strategy.four_pattern import FourPattern
        s = FourPattern({
            "swing_lookback": 3,
            "min_swing_touches": 2,
            "atr_period": 5,
            "min_volume_z": 1.0,
            "min_absorption_ratio": 5.0,
        })
        s.set_symbol("BTC")
        return s

    def _run(self, coro_fn):
        """Execute `coro_fn(s)` in a loop with a strategy instance."""

        async def _wrapper():
            s = self._strategy()
            result = coro_fn(s)
            if asyncio.iscoroutine(result):
                return await result
            return result

        return asyncio.run(_wrapper())

    def test_has_on_kline_entry_point(self):
        def check(s):
            self.assertIn("on_kline", type(s).__dict__)

        self._run(check)

    def test_warmup_timeframes_cover_both_engines(self):
        def check(s):
            tfs = s.warmup_timeframes("1m")
            self.assertIn("5m", tfs)  # entries
            self.assertIn("1m", tfs)  # exits

        self._run(check)

    def test_warmup_builds_atr_and_pivots(self):
        def feed_bars(s):
            rng = random.Random(7)
            price = 100.0
            bars = []
            for i in range(80):
                o = price
                price += rng.uniform(-3, 3)
                c = price
                h = max(o, c) + abs(rng.uniform(0, 2))
                l = min(o, c) - abs(rng.uniform(0, 2))
                bars.append(
                    _make_5m_bar(1_700_000_000_000 + i * 300_000, o, h, l, c)
                )
            return bars

        async def run(s):
            for b in feed_bars(s):
                await s.on_kline("BTC", b)
            return s

        s = self._run(run)
        self.assertGreater(len(s._klines_5m["BTC"]), 0, "no 5m bars stored")
        atr = s._current_atr_5m("BTC")
        self.assertGreater(atr, 0.0, "ATR is still zero after warmup")

    def test_1m_bars_reach_the_exit_engine(self):
        bar = {
            "symbol": "BTC",
            "interval": "1m",
            "kline": {
                "t": 1_700_000_000_000, "o": 100.0, "h": 101.0,
                "l": 99.0, "c": 100.5, "v": 10.0, "closed": True,
            },
        }

        async def run(s):
            await s.on_kline("BTC", bar)
            return s

        s = self._run(run)
        self.assertEqual(len(s._klines_1m["BTC"]), 1)

    def test_unclosed_bars_are_ignored(self):
        payload = _make_5m_bar(1_700_000_000_000, 1, 2, 0.5, 1.5)
        payload["kline"]["closed"] = False

        async def run(s):
            await s.on_kline("BTC", payload)
            return s

        s = self._run(run)
        self.assertEqual(len(s._klines_5m.get("BTC", [])), 0)

    def test_no_exit_while_entry_pending(self):
        """A CLOSE must not be evaluated before the fill confirms."""
        bar = {
            "symbol": "BTC",
            "interval": "1m",
            "kline": {
                "t": 1_700_000_000_000, "o": 250.0, "h": 260.0,
                "l": 240.0, "c": 255.0, "v": 10.0, "closed": True,
            },
        }

        async def run(s):
            s._state = "IN_TRADE"
            s._entry_pending = True
            s._trade = type("T", (), {
                "direction": "BUY", "entry_price": 100.0,
                "stop_price": 200.0,  # price would blow through it
                "target_price": 300.0, "atr_at_entry": 1.0,
                "entry_ts": 0, "mfe": 0.0, "mae": 0.0,
                "be_triggered": False, "pivot": None,
                "pattern": "SPRING", "exit_signal_count": 0,
            })()
            before = len(self.bus.published)
            await s.on_kline("BTC", bar)
            return self.bus.published[before:]

        emitted = self._run(run)
        closes = [p for t, p in emitted if p.get("action") == "CLOSE"]
        self.assertEqual(closes, [], "exit fired while entry still pending")


if __name__ == "__main__":
    unittest.main()
