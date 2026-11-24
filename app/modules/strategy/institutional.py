"""Institutional-grade primitives shared by strategies.

Every primitive is stateful, reset-safe, and disposable so runtime
strategy swaps don't leak memory or leave orphan callbacks.

Design principles:
  - No hardcoded magic numbers where data can calibrate instead
    (percentiles, ATR multiples, markout distributions).
  - Every decision is measurable (MFE/MAE both sides, VPIN, fee
    gate margins) so signal health can be tracked objectively.
  - Never break-even — trail volatility-scaled from the peak.
  - Size by risk (ATR-scaled), never by fixed notional.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal


def _now_ms() -> int:
    return int(time.time() * 1000)


# ═══════════════════════════════════════════════════════════
# 1. PercentileThreshold
# ═══════════════════════════════════════════════════════════

class PercentileThreshold:
    """Rolling percentile band, adaptive to market conditions.

    Replaces hardcoded thresholds (e.g. "L1 imbalance 18x-55x") with
    data-driven bands. Institutional signals live in the 60-90th
    percentile of typical conditions, not at 99th-percentile outliers.

    Args:
        window: rolling sample count
        low_pct: lower band edge (below → suppress)
        high_pct: upper band edge (above → suppress)
        min_samples: samples required before state != "warming"
    """

    def __init__(
        self,
        window: int = 500,
        low_pct: float = 60.0,
        high_pct: float = 90.0,
        min_samples: int = 50,
    ) -> None:
        self.window = max(10, window)
        self.low_pct = float(low_pct)
        self.high_pct = float(high_pct)
        self.min_samples = int(min_samples)
        self._buf: deque[float] = deque(maxlen=self.window)

    def reset(self) -> None:
        self._buf.clear()

    def update(self, value: float) -> None:
        if value is None or math.isnan(value):
            return
        self._buf.append(float(value))

    def _sorted(self) -> list[float]:
        return sorted(self._buf)

    def pct_of(self, value: float) -> float:
        """Return percentile rank (0-100) of `value` in current window."""
        if not self._buf or value is None:
            return 50.0
        arr = self._sorted()
        n = len(arr)
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid] < value:
                lo = mid + 1
            else:
                hi = mid
        return (lo / n) * 100.0

    def bounds(self) -> tuple[float, float]:
        """Return (low_value, high_value) of current percentile band."""
        if len(self._buf) < self.min_samples:
            return (-math.inf, math.inf)
        arr = self._sorted()
        n = len(arr)
        lo = max(0, min(n - 1, int(n * self.low_pct / 100.0)))
        hi = max(0, min(n - 1, int(n * self.high_pct / 100.0)))
        return arr[lo], arr[hi]

    def state(self, value: float) -> Literal["below", "in", "above", "warming"]:
        if len(self._buf) < self.min_samples:
            return "warming"
        lo, hi = self.bounds()
        if value < lo:
            return "below"
        if value > hi:
            return "above"
        return "in"

    def describe(self, value: float) -> str:
        """Human-readable: 'p{XX} [{lo}..{hi}] {state}'."""
        if len(self._buf) < self.min_samples:
            return "warming"
        p = self.pct_of(value)
        lo, hi = self.bounds()
        return f"p{p:.0f} [{lo:.2f}..{hi:.2f}] {self.state(value)}"


# ═══════════════════════════════════════════════════════════
# 2. VolatilitySizer
# ═══════════════════════════════════════════════════════════

class VolatilitySizer:
    """ATR-scaled position sizing (institutional risk model).

    size = min(
        risk_pct * equity / (atr * risk_mult),   # risk-based
        max_position_pct * equity / price,       # leverage cap
        symbol_max_volume,
    )
    Floor at symbol_min_volume.

    Falls back to `fallback_volume` until `equity > 0` (i.e., before
    the first `account_update` event).
    """

    def __init__(
        self,
        risk_pct: float = 0.005,
        max_position_pct: float = 0.20,
        risk_mult: float = 1.5,
        fallback_volume: float = 0.01,
        min_volume: float = 0.01,
        max_volume: float = 100.0,
    ) -> None:
        self.risk_pct = float(risk_pct)
        self.max_position_pct = float(max_position_pct)
        self.risk_mult = float(risk_mult)
        self.fallback_volume = float(fallback_volume)
        self.min_volume = float(min_volume)
        self.max_volume = float(max_volume)
        self._equity: float = 0.0

    def set_equity(self, equity: float) -> None:
        try:
            v = float(equity)
        except (TypeError, ValueError):
            return
        if v > 0:
            self._equity = v

    def compute(
        self,
        atr: float,
        price: float,
        risk_mult: float | None = None,
    ) -> float:
        if self._equity <= 0 or atr <= 0 or price <= 0:
            return self._clamp(self.fallback_volume)

        rm = float(risk_mult) if risk_mult is not None else self.risk_mult
        risk_usd = self.risk_pct * self._equity
        stop_dist = atr * max(0.1, rm)
        size_by_risk = risk_usd / stop_dist
        size_by_cap = self.max_position_pct * self._equity / price
        size = min(size_by_risk, size_by_cap)
        return self._clamp(size)

    def _clamp(self, size: float) -> float:
        size = max(self.min_volume, min(self.max_volume, size))
        return round(size, 4)


# ═══════════════════════════════════════════════════════════
# 3. ATRTrailer
# ═══════════════════════════════════════════════════════════

class ATRTrailer:
    """Volatility-scaled trailing stop.

    No break-even — BE-stop converts winners to scratches, one of
    the most well-documented edge leaks in systematic trading.

    Once profit_r >= activation_r:
        new_stop = peak_price - trail_mult * atr   (for long)
        new_stop = peak_price + trail_mult * atr   (for short)

    Stop is monotonic: only ever advances in the favorable direction.
    """

    def __init__(
        self,
        activation_r: float = 1.5,
        trail_mult: float = 2.0,
    ) -> None:
        self.activation_r = float(activation_r)
        self.trail_mult = float(trail_mult)

    def update(
        self,
        side: str,
        entry: float,
        peak_price: float,
        current_stop: float,
        atr: float,
        risk_at_entry: float,
    ) -> float:
        """Return the new stop. Never loosens."""
        if atr <= 0 or risk_at_entry <= 0 or entry <= 0:
            return current_stop

        is_long = side == "BUY"
        if is_long:
            profit_r = (peak_price - entry) / risk_at_entry
        else:
            profit_r = (entry - peak_price) / risk_at_entry

        if profit_r < self.activation_r:
            return current_stop

        offset = self.trail_mult * atr
        if is_long:
            candidate = peak_price - offset
            return max(current_stop, candidate) if current_stop > 0 else candidate
        else:
            candidate = peak_price + offset
            if current_stop <= 0:
                return candidate
            return min(current_stop, candidate)


# ═══════════════════════════════════════════════════════════
# 4. MarkoutTracker (both-side adverse selection)
# ═══════════════════════════════════════════════════════════

@dataclass
class _PendingMarkout:
    signal_id: str
    symbol: str
    side: str
    signal_type: str
    entry_price: float
    entry_ts: int
    mid_at: dict[int, float] = field(default_factory=dict)
    adverse_at: dict[int, float] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return len(self.mid_at) >= 5


class MarkoutTracker:
    """Tracks MFE and MAE at multiple horizons for every entry.

    Favorable markout: (mid - entry) * side_sign  — good for us
    Adverse markout:   (entry - mid) * side_sign  — bad for us

    Both are recorded because adverse-selection (getting filled
    right before a strong move against us) is the dominant source
    of HFT edge decay and is invisible if we only track favorable.

    `drain_completed()` yields finished markouts for the strategy
    to persist. `adverse_ratio()` exposes live health.
    """

    HORIZONS_MS: tuple[int, ...] = (100, 500, 1000, 5000, 30000)

    def __init__(self, max_pending: int = 200) -> None:
        self._pending: list[_PendingMarkout] = []
        self._completed: list[dict[str, Any]] = []
        self._max_pending = max(10, max_pending)
        # Rolling worst-adverse per horizon (for health)
        self._adv_hist: dict[int, deque[float]] = {
            h: deque(maxlen=200) for h in self.HORIZONS_MS
        }
        self._fav_hist: dict[int, deque[float]] = {
            h: deque(maxlen=200) for h in self.HORIZONS_MS
        }

    def reset(self) -> None:
        self._pending.clear()
        self._completed.clear()
        for d in self._adv_hist.values():
            d.clear()
        for d in self._fav_hist.values():
            d.clear()

    def schedule(
        self,
        signal_id: str,
        symbol: str,
        side: str,
        signal_type: str,
        entry_price: float,
        entry_ts: int,
    ) -> None:
        if len(self._pending) >= self._max_pending:
            self._pending.pop(0)
        self._pending.append(_PendingMarkout(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            signal_type=signal_type,
            entry_price=float(entry_price),
            entry_ts=int(entry_ts),
        ))

    def update_mid(self, mid: float, now_ms: int | None = None) -> None:
        if mid <= 0 or not self._pending:
            return
        now = now_ms if now_ms is not None else _now_ms()

        for p in self._pending:
            elapsed = now - p.entry_ts
            sign = 1.0 if p.side == "BUY" else -1.0
            for h in self.HORIZONS_MS:
                if h in p.mid_at or elapsed < h:
                    continue
                p.mid_at[h] = mid
                signed_move = (mid - p.entry_price) * sign
                p.adverse_at[h] = max(0.0, -signed_move)

            if p.complete:
                self._finalize(p)

        self._pending = [p for p in self._pending if not p.complete]

    def _finalize(self, p: _PendingMarkout) -> None:
        row: dict[str, Any] = {
            "signal_id": p.signal_id,
            "symbol": p.symbol,
            "side": p.side,
            "signal_type": p.signal_type,
            "entry_ts": p.entry_ts,
            "entry_price": p.entry_price,
        }
        sign = 1.0 if p.side == "BUY" else -1.0
        for h in self.HORIZONS_MS:
            key = f"{h}ms" if h < 1000 else f"{h // 1000}s"
            mid_v = p.mid_at.get(h)
            row[f"mid_{key}"] = mid_v
            if mid_v is not None:
                fav = (mid_v - p.entry_price) * sign
                row[f"markout_{key}"] = round(fav, 6)
                row[f"adverse_{key}"] = round(p.adverse_at.get(h, 0.0), 6)
                self._fav_hist[h].append(fav)
                self._adv_hist[h].append(p.adverse_at.get(h, 0.0))
            else:
                row[f"markout_{key}"] = None
                row[f"adverse_{key}"] = None
        self._completed.append(row)

    def drain_completed(self) -> list[dict[str, Any]]:
        out = self._completed
        self._completed = []
        return out

    def adverse_ratio(self, horizon_ms: int = 1000) -> float:
        """Mean adverse / mean favorable at a horizon. > 1 = losing edge."""
        if horizon_ms not in self._adv_hist:
            return 0.0
        adv = list(self._adv_hist[horizon_ms])
        fav = list(self._fav_hist[horizon_ms])
        if not adv or not fav:
            return 0.0
        mean_adv = sum(adv) / len(adv)
        mean_fav = sum(fav) / len(fav)
        if mean_fav <= 0:
            return 10.0
        return mean_adv / mean_fav

    def mean_favorable(self, horizon_ms: int = 1000) -> float:
        d = self._fav_hist.get(horizon_ms)
        if not d:
            return 0.0
        return sum(d) / len(d)


# ═══════════════════════════════════════════════════════════
# 5. FeeModel
# ═══════════════════════════════════════════════════════════

class FeeModel:
    """Fee + spread aware expected-move gate.

    Entry only if the expected move clears round-trip transaction
    costs by a configurable multiple. Institutional desks require
    2-3x the round-trip fee as minimum edge.
    """

    def __init__(
        self,
        taker_fee_bps: float = 5.0,
        edge_mult: float = 2.0,
    ) -> None:
        self.taker_fee_bps = float(taker_fee_bps)
        self.edge_mult = float(edge_mult)

    def min_expected_bps(self, spread_bps: float = 0.0) -> float:
        round_trip = 2.0 * self.taker_fee_bps + max(0.0, spread_bps)
        return round_trip * self.edge_mult

    def should_enter(
        self,
        expected_move_bps: float,
        spread_bps: float = 0.0,
    ) -> tuple[bool, str]:
        required = self.min_expected_bps(spread_bps)
        if expected_move_bps < required:
            return False, (
                f"fee_gate ({expected_move_bps:.1f}bps "
                f"< {required:.1f}bps required)"
            )
        return True, ""


# ═══════════════════════════════════════════════════════════
# 6. VPINToxicity (volume-bucketed, proper)
# ═══════════════════════════════════════════════════════════

class VPINToxicity:
    """Volume-Synchronized Probability of Informed Trading.

    Buckets trades by CUMULATIVE VOLUME, not time — matching
    Easley, Lopez de Prado, O'Hara (2012). Time-bucketing breaks
    the theory because it over-weights quiet periods.

    toxicity = mean(|buy_vol - sell_vol| / total_vol) across buckets
    Range [0, 1]. Higher = more informed flow.
    """

    def __init__(
        self,
        bucket_volume: float = 1.0,
        max_buckets: int = 50,
        min_buckets: int = 5,
    ) -> None:
        self.bucket_volume = float(bucket_volume)
        self.max_buckets = int(max_buckets)
        self.min_buckets = int(min_buckets)
        self._buckets: deque[tuple[float, float]] = deque(maxlen=self.max_buckets)
        self._cur_buy = 0.0
        self._cur_sell = 0.0

    def reset(self) -> None:
        self._buckets.clear()
        self._cur_buy = 0.0
        self._cur_sell = 0.0

    def set_bucket_volume(self, bucket_volume: float) -> None:
        if bucket_volume > 0:
            self.bucket_volume = float(bucket_volume)

    def add_trade(self, qty: float, signed: float) -> None:
        if qty <= 0:
            return
        if signed > 0:
            self._cur_buy += qty
        else:
            self._cur_sell += qty

        while (self._cur_buy + self._cur_sell) >= self.bucket_volume:
            remaining = self.bucket_volume
            buy_take = min(self._cur_buy, remaining)
            self._cur_buy -= buy_take
            remaining -= buy_take
            sell_take = min(self._cur_sell, remaining)
            self._cur_sell -= sell_take
            self._buckets.append((buy_take, sell_take))

    def toxicity(self) -> float:
        if len(self._buckets) < self.min_buckets:
            return 0.0
        total_imbalance = 0.0
        total_vol = 0.0
        for buy, sell in self._buckets:
            total_imbalance += abs(buy - sell)
            total_vol += buy + sell
        return total_imbalance / total_vol if total_vol > 0 else 0.0


# ═══════════════════════════════════════════════════════════
# 7. SignalHealthTracker
# ═══════════════════════════════════════════════════════════

class SignalHealthTracker:
    """Enables/disables signal types based on realized markout edge.

    If a signal type's rolling mean markout turns persistently
    negative, disable it. Re-enable when edge recovers.
    """

    def __init__(
        self,
        window: int = 200,
        disable_threshold: float = -0.0005,
        reenable_threshold: float = 0.0001,
        min_samples: int = 40,
    ) -> None:
        self.window = int(window)
        self.disable_threshold = float(disable_threshold)
        self.reenable_threshold = float(reenable_threshold)
        self.min_samples = int(min_samples)
        self._by_type: dict[str, deque[float]] = {}
        self._disabled: set[str] = set()

    def reset(self) -> None:
        self._by_type.clear()
        self._disabled.clear()

    def record(self, signal_type: str, markout: float) -> None:
        buf = self._by_type.setdefault(
            signal_type, deque(maxlen=self.window),
        )
        buf.append(float(markout))
        if len(buf) < self.min_samples:
            return
        mean = sum(buf) / len(buf)
        if mean < self.disable_threshold:
            self._disabled.add(signal_type)
        elif mean > self.reenable_threshold:
            self._disabled.discard(signal_type)

    def is_enabled(self, signal_type: str) -> bool:
        return signal_type not in self._disabled

    def mean_markout(self, signal_type: str) -> float:
        buf = self._by_type.get(signal_type)
        if not buf:
            return 0.0
        return sum(buf) / len(buf)


# ═══════════════════════════════════════════════════════════
# 8. SessionFilter
# ═══════════════════════════════════════════════════════════

class SessionFilter:
    """Time-of-day trading window filter.

    Skips the first N minutes and last M minutes of the UTC day
    where liquidity is thin and false breakouts cluster. Optionally
    excludes weekends for stocks; for crypto, keep weekends but
    note volatility may be lower.
    """

    def __init__(
        self,
        skip_open_min: int = 5,
        skip_close_min: int = 5,
        session_start_hour_utc: int = 0,
        session_end_hour_utc: int = 24,
    ) -> None:
        self.skip_open_min = int(skip_open_min)
        self.skip_close_min = int(skip_close_min)
        self.start_hour = int(session_start_hour_utc)
        self.end_hour = int(session_end_hour_utc)

    def is_tradeable(self, now_ms: int | None = None) -> bool:
        now = now_ms if now_ms is not None else _now_ms()
        sec_of_day = (now // 1000) % 86400
        hour = sec_of_day // 3600
        min_of_hour = (sec_of_day % 3600) // 60

        # Outside declared session
        if not (self.start_hour <= hour < self.end_hour):
            return False

        # First N min after session open
        if hour == self.start_hour and min_of_hour < self.skip_open_min:
            return False

        # Last M min before session close
        close_hour = self.end_hour - 1
        if hour == close_hour and min_of_hour >= (60 - self.skip_close_min):
            return False

        return True

    def reason_if_blocked(self, now_ms: int | None = None) -> str:
        if self.is_tradeable(now_ms):
            return ""
        return "session_filter"

# ═══════════════════════════════════════════════════════════
# 9. ParabolicSARTracker
# ═══════════════════════════════════════════════════════════
class ParabolicSARTracker:
    """Parabolic SAR trailing stop tracker."""
    def __init__(self, step: float = 0.02, max_step: float = 0.20):
        self.step = step
        self.max_step = max_step
        self.reset()

    def reset(self, trend: int = 1) -> None:
        self.sar = 0.0
        self.ep = 0.0
        self.af = self.step
        self.trend = trend # 1 = up, -1 = down
        self.prev_high = 0.0
        self.prev_low = 0.0
        self.initialized = False

    def update(self, high: float, low: float) -> float:
        if not self.initialized:
            if self.trend == 1:
                self.sar = low
                self.ep = high
            else:
                self.sar = high
                self.ep = low
            self.af = self.step
            self.prev_high = high
            self.prev_low = low
            self.initialized = True
            return self.sar

        next_sar = self.sar + self.af * (self.ep - self.sar)

        if self.trend == 1: # Uptrend
            if high > self.ep:
                self.ep = high
                self.af = min(self.af + self.step, self.max_step)
            
            # SAR cannot be higher than the prior low
            next_sar = min(next_sar, self.prev_low)
            
            if low < next_sar:
                self.trend = -1
                self.sar = self.ep
                self.ep = low
                self.af = self.step
            else:
                self.sar = next_sar
        else: # Downtrend
            if low < self.ep:
                self.ep = low
                self.af = min(self.af + self.step, self.max_step)

            # SAR cannot be lower than the prior high
            next_sar = max(next_sar, self.prev_high)

            if high > next_sar:
                self.trend = 1
                self.sar = self.ep
                self.ep = high
                self.af = self.step
            else:
                self.sar = next_sar

        self.prev_high = high
        self.prev_low = low
        return self.sar