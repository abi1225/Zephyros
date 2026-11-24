"""Order-flow metric computation for the four_pattern strategy.

Pure functions only — the strategy owns all state, this module
computes scalars from klines + trades buffers.

Metrics implemented (Tier 1 from the document):
  - ATR
  - Delta (buy_vol - sell_vol over a time window)
  - CVD series + slope + divergence
  - Displacement (bar body / ATR)
  - Delta efficiency (|ΔPrice| / |delta|)
  - Absorption ratio (volume / |displacement|, ATR-normalized)
  - RVOL and volume Z-score
  - VWAP
  - POC and POC migration
  - Liquidity sweep detection
  - Follow-through detection
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


# ─── ATR ──────────────────────────────────────────────────


def compute_atr(klines: Sequence[Bar], period: int = 14) -> float:
    if len(klines) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(klines)):
        prev_c = klines[i - 1].c
        h, l = klines[i].h, klines[i].l
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = ((atr * (period - 1)) + tr) / period
    return atr


# ─── Delta / CVD ──────────────────────────────────────────


def compute_delta(
    trades: Iterable[tuple[int, float, float]],
    start_ts: int,
    end_ts: int,
) -> tuple[float, float, float]:
    """Return (buy_vol, sell_vol, delta) for trades in [start_ts, end_ts)."""
    buy = 0.0
    sell = 0.0
    for ts, _price, signed in trades:
        if ts < start_ts:
            continue
        if ts >= end_ts:
            break
        if signed > 0:
            buy += signed
        else:
            sell += -signed
    return buy, sell, buy - sell


def compute_cvd_series(
    trades: Sequence[tuple[int, float, float]],
    window_ms: int,
    end_ts: int,
    buckets: int = 6,
) -> list[float]:
    """CVD at end of each of the last `buckets` equal sub-windows."""
    if not trades or buckets <= 0:
        return []
    bucket_ms = window_ms // buckets
    if bucket_ms <= 0:
        return []
    start = end_ts - window_ms
    series: list[float] = []
    running = 0.0
    bucket_start = start
    for _ in range(buckets):
        bucket_end = bucket_start + bucket_ms
        for ts, _p, signed in trades:
            if ts < bucket_start:
                continue
            if ts >= bucket_end:
                break
            running += signed
        series.append(running)
        bucket_start = bucket_end
    return series


def compute_cvd_slope(series: list[float]) -> float:
    if len(series) < 2:
        return 0.0
    return (series[-1] - series[0]) / (len(series) - 1)


def detect_cvd_divergence(
    bars: Sequence[Bar],
    cvd_series: list[float],
    direction: str,
) -> bool:
    """Price new extreme, CVD fails to confirm.

    direction == "BUY" → check for bearish divergence at a price high
      (price higher high, CVD lower high) — signals exhaustion of uptrend.
    direction == "SELL" → check for bullish divergence at a price low.
    """
    if len(bars) < 3 or len(cvd_series) < 3:
        return False
    if direction == "BUY":
        recent_high = max(b.h for b in bars[-2:])
        prior_high = max(b.h for b in bars[:-2])
        if recent_high <= prior_high:
            return False
        recent_cvd = max(cvd_series[-2:])
        prior_cvd = max(cvd_series[:-2])
        return recent_cvd < prior_cvd
    recent_low = min(b.l for b in bars[-2:])
    prior_low = min(b.l for b in bars[:-2])
    if recent_low >= prior_low:
        return False
    recent_cvd = min(cvd_series[-2:])
    prior_cvd = min(cvd_series[:-2])
    return recent_cvd > prior_cvd


# ─── Per-bar metrics ──────────────────────────────────────


def compute_displacement_atr(bar: Bar, atr: float) -> float:
    if atr <= 0:
        return 0.0
    return abs(bar.c - bar.o) / atr


def compute_delta_efficiency(
    price_start: float, price_end: float, delta: float,
) -> float:
    if abs(delta) < 1e-9:
        return 0.0
    return abs(price_end - price_start) / abs(delta)


def compute_absorption_ratio(bar: Bar, atr: float) -> float:
    """Volume / |body in ATR units|.

    High = heavy volume, small price move = absorption.
    Low = clean directional move.
    """
    if atr <= 0 or bar.v <= 0:
        return 0.0
    body_atr = abs(bar.c - bar.o) / atr
    if body_atr < 0.05:
        return 100.0  # very tight bar with volume → maximal absorption
    return bar.v / body_atr


# ─── Participation ────────────────────────────────────────


def compute_rvol(bars: Sequence[Bar], lookback: int = 20) -> float:
    if len(bars) < 2:
        return 1.0
    recent = bars[-lookback:]
    if len(recent) < 2:
        return 1.0
    avg = sum(b.v for b in recent[:-1]) / (len(recent) - 1)
    if avg <= 0:
        return 1.0
    return recent[-1].v / avg


def compute_volume_z(bars: Sequence[Bar], lookback: int = 20) -> float:
    if len(bars) < 3:
        return 0.0
    recent = bars[-lookback:]
    if len(recent) < 3:
        return 0.0
    vols = [b.v for b in recent]
    current = vols[-1]
    prior = vols[:-1]
    mean = sum(prior) / len(prior)
    if len(prior) < 2:
        return 0.0
    var = sum((v - mean) ** 2 for v in prior) / (len(prior) - 1)
    std = var ** 0.5
    if std <= 0:
        return 0.0
    return (current - mean) / std


# ─── VWAP / POC ───────────────────────────────────────────


def compute_vwap(bars: Sequence[Bar], lookback: int = 20) -> float:
    if not bars:
        return 0.0
    recent = bars[-lookback:]
    total_v = 0.0
    total_pv = 0.0
    for b in recent:
        tp = (b.h + b.l + b.c) / 3.0
        total_pv += tp * b.v
        total_v += b.v
    if total_v <= 0:
        return 0.0
    return total_pv / total_v


def compute_poc(
    bars: Sequence[Bar], lookback: int = 60, bins: int = 20,
) -> float:
    """Point of Control — price bin with highest traded volume."""
    if not bars:
        return 0.0
    recent = bars[-lookback:]
    if len(recent) < 3:
        return 0.0
    pmin = min(b.l for b in recent)
    pmax = max(b.h for b in recent)
    if pmax <= pmin:
        return pmin
    step = (pmax - pmin) / bins
    if step <= 0:
        return (pmin + pmax) / 2.0
    hist = [0.0] * bins
    for b in recent:
        lo_idx = max(0, min(bins - 1, int((b.l - pmin) / step)))
        hi_idx = max(0, min(bins - 1, int((b.h - pmin) / step)))
        n_bins = hi_idx - lo_idx + 1
        v_per_bin = b.v / n_bins
        for i in range(lo_idx, hi_idx + 1):
            hist[i] += v_per_bin
    max_idx = hist.index(max(hist))
    return pmin + (max_idx + 0.5) * step


def compute_poc_migration(
    bars: Sequence[Bar], lookback: int = 60, window: int = 6,
) -> float:
    """POC now minus POC `window` bars ago. Positive = migrating up."""
    if len(bars) < window + 3:
        return 0.0
    poc_now = compute_poc(bars, lookback=lookback)
    poc_prev = compute_poc(bars[:-window], lookback=lookback)
    return poc_now - poc_prev


# ─── Sweep / follow-through ───────────────────────────────


def detect_sweep(
    bar: Bar,
    pivot_price: float,
    direction: str,
    atr: float,
    min_wick_atr: float = 0.5,
) -> bool:
    """Liquidity sweep + reclaim pattern.

    direction == "BUY": wick below pivot, close above pivot,
      wick depth >= min_wick_atr * ATR.
    direction == "SELL": mirror.
    """
    if atr <= 0:
        return False
    if direction == "BUY":
        if bar.l >= pivot_price or bar.c <= pivot_price:
            return False
        return (pivot_price - bar.l) >= min_wick_atr * atr
    if bar.h <= pivot_price or bar.c >= pivot_price:
        return False
    return (bar.h - pivot_price) >= min_wick_atr * atr


def detect_follow_through(
    prev_bar: Bar,
    current_bar: Bar,
    pivot_price: float,
    direction: str,
) -> bool:
    """Both bars closed on the correct side of the pivot."""
    if direction == "BUY":
        return prev_bar.c > pivot_price and current_bar.c > pivot_price
    return prev_bar.c < pivot_price and current_bar.c < pivot_price