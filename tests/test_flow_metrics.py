"""Unit tests for the pure order-flow metrics.

These are dependency-free: run with `python -m unittest discover tests`
from the project root. No MT5, no Qt, no network.
"""
from __future__ import annotations

import unittest

from app.modules.strategy.flow_metrics import (
    Bar,
    compute_atr,
    compute_delta,
    detect_cvd_divergence,
    detect_follow_through,
    detect_sweep,
)

def bar(o, h, l, c, v=1000.0, t=0):
    return Bar(t=t, o=o, h=h, l=l, c=c, v=v)


class TestCvdDivergence(unittest.TestCase):
    """Regression tests for the inverted-divergence bug.

    `detect_cvd_divergence` used to take the TRADE DIRECTION, which made
    an entry confirmation look for the divergence that opposed the trade.
    It now takes an explicit `kind` ("bullish" | "bearish").
    """

    def test_bullish_divergence_at_low(self):
        # Price makes a lower low, CVD makes a higher low.
        bars = [bar(10, 11, 9, 10), bar(10, 10.5, 8.0, 8.5), bar(8.5, 9, 7.0, 7.5)]
        cvd = [-5.0, -1.0, 1.0]
        self.assertTrue(detect_cvd_divergence(bars, cvd, "bullish"))

    def test_bearish_divergence_at_high(self):
        # Price makes a higher high, CVD makes a lower high.
        bars = [bar(10, 11, 10, 10.5), bar(10.5, 12, 10.2, 11.8), bar(11.8, 13, 11.5, 12.8)]
        cvd = [5.0, 4.0, 3.0]
        self.assertTrue(detect_cvd_divergence(bars, cvd, "bearish"))

    def test_kinds_are_not_symmetric(self):
        lower_low = [bar(10, 11, 9, 10), bar(10, 10.5, 8.0, 8.5), bar(8.5, 9, 7.0, 7.5)]
        cvd = [-5.0, -1.0, 1.0]
        self.assertFalse(detect_cvd_divergence(lower_low, cvd, "bearish"))

        higher_high = [bar(10, 11, 10, 10.5), bar(10.5, 12, 10.2, 11.8), bar(11.8, 13, 11.5, 12.8)]
        cvd2 = [5.0, 4.0, 3.0]
        self.assertFalse(detect_cvd_divergence(higher_high, cvd2, "bullish"))

    def test_unknown_kind_is_false_not_truthy(self):
        # Guards against a caller still passing "BUY"/"SELL".
        bars = [bar(10, 11, 9, 10), bar(10, 10.5, 8.0, 8.5), bar(8.5, 9, 7.0, 7.5)]
        cvd = [-5.0, -1.0, 1.0]
        self.assertFalse(detect_cvd_divergence(bars, cvd, "BUY"))
        self.assertFalse(detect_cvd_divergence(bars, cvd, "SELL"))

    def test_too_short_series(self):
        self.assertFalse(detect_cvd_divergence([bar(1, 2, 1, 1)], [-1.0], "bullish"))


class TestAtr(unittest.TestCase):
    def test_insufficient_bars_returns_zero(self):
        self.assertEqual(compute_atr([bar(1, 2, 1, 2)] * 5, period=14), 0.0)

    def test_constant_range(self):
        bars = [bar(100, 101, 99, 100, t=i * 1000) for i in range(20)]
        self.assertAlmostEqual(compute_atr(bars, period=14), 2.0, places=6)


class TestDelta(unittest.TestCase):
    def test_delta_window(self):
        trades = [(1000, 100.0, 5.0), (2000, 100.0, -3.0), (5000, 100.0, 2.0)]
        buy, sell, delta = compute_delta(trades, 1000, 3000)
        self.assertAlmostEqual(buy, 5.0)
        self.assertAlmostEqual(sell, 3.0)
        self.assertAlmostEqual(delta, 2.0)


class TestSweep(unittest.TestCase):
    def test_bullish_sweep(self):
        # Wick below the pivot, close back above it.
        b = bar(100, 101, 97, 100.5)
        self.assertTrue(detect_sweep(b, 99.0, "BUY", atr=2.0, min_wick_atr=0.5))

    def test_no_sweep_when_close_stays_below(self):
        b = bar(100, 101, 97, 98.0)
        self.assertFalse(detect_sweep(b, 99.0, "BUY", atr=2.0, min_wick_atr=0.5))


class TestFollowThrough(unittest.TestCase):
    def test_both_bars_above_pivot(self):
        self.assertTrue(detect_follow_through(bar(100, 103, 100, 102),
                                             bar(102, 104, 101, 103),
                                             101.0, "BUY"))

    def test_second_bar_fails(self):
        self.assertFalse(detect_follow_through(bar(100, 103, 100, 102),
                                               bar(102, 103, 99, 100),
                                               101.0, "BUY"))


if __name__ == "__main__":
    unittest.main()
