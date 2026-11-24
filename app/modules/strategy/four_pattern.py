"""Four-Pattern Momentum Strategy (pivot-level, dual-timeframe).

Entry fires on 5-minute candle close. Exit fires on 1-minute candle close.

Four patterns (all entered on 5m close after regime+confirmation+veto gates):
  SPRING               — pivot low holds, buyers reclaim       → BUY
  FAILED_RALLY         — pivot high holds, sellers reclaim     → SELL
  FAILED_DEFENSE_UP    — pivot high breaks up, buyers push     → BUY
  FAILED_DEFENSE_DOWN  — pivot low breaks down, sellers push   → SELL

Exit cascade (evaluated on every 1m close while in-trade):
  1. Hard stop         — structural + breathing + cap
  2. Target            — next pivot (ATR-bounded)
  3. Time stop         — t_max_ms
  4. Breakeven shift   — at +1 ATR profit, move stop to BE+buffer
  5. Exhaustion exit   — regime turned AND confluence ≥2 AND 2-bar persistence
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.core.config import config
from app.core.event_bus import bus
from app.modules.strategy.base import Signal, Strategy
from app.modules.strategy.flow_metrics import (
    Bar,
    compute_absorption_ratio,
    compute_atr,
    compute_cvd_series,
    compute_delta,
    compute_delta_efficiency,
    compute_displacement_atr,
    compute_poc_migration,
    compute_rvol,
    compute_volume_z,
    compute_vwap,
    detect_cvd_divergence,
    detect_follow_through,
    detect_sweep,
)

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


_FIVE_MIN_MS = 300_000
_ONE_MIN_MS = 60_000


# ─── Data structures ──────────────────────────────────────


@dataclass
class Pivot:
    price: float
    side: str  # "support" | "resistance"
    touches: int = 0
    detected_ts: int = 0


@dataclass
class TradeContext:
    pattern: str = ""
    direction: str = ""
    pivot: Pivot | None = None
    entry_price: float = 0.0
    entry_ts: int = 0
    stop_price: float = 0.0
    target_price: float = 0.0
    original_stop: float = 0.0
    be_triggered: bool = False
    mfe: float = 0.0
    mae: float = 0.0
    atr_at_entry: float = 0.0
    exit_signal_count: int = 0
    entry_confirmations: dict = field(default_factory=dict)


# ─── Strategy ─────────────────────────────────────────────


class FourPattern(Strategy):
    name = "four_pattern"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._load_base_defaults()

        # Runtime state
        self._state = "IDLE"
        self._state_ts = _now_ms()
        self._active_pivot: Pivot | None = None
        self._trade: TradeContext | None = None
        self._entry_pending = False
        self._pending_entry_ts = 0
        self._last_entry_eval_ts: dict[str, int] = {}

        # Buffers
        self._klines_1m: dict[str, deque] = {}
        self._klines_5m: dict[str, deque] = {}
        self._delta_5m: dict[str, deque] = {}
        self._trades: dict[str, deque] = {}
        self._open_5m: dict[str, dict] = {}
        self._swings_cache: dict[str, tuple[int, list[Pivot]]] = {}

        # Subscriptions
        self._topic_feed_trade = config.protocol.topics.get(
            "feed_trade", "feed.trade"
        )
        self._topic_position = config.protocol.topics.get(
            "position_update", "position.update"
        )
        self._topic_debug = config.protocol.topics.get(
            "debug_decision", "debug.decision"
        )
        self._topic_signal_failed = config.protocol.topics.get(
            "signal_failed", "signal_failed"
        )

        bus.subscribe(self._topic_feed_trade, self._on_trade)
        bus.subscribe(self._topic_position, self._on_position)
        bus.subscribe(self._topic_signal_failed, self._on_signal_failed)
        # NOTE: klines are delivered through StrategyManager.on_kline ->
        # FourPattern.on_kline (single path). The strategy must NOT also
        # subscribe to feed_kline, or every bar is processed twice.

    # ─── Params ────────────────────────────────────────────

    def _load_base_defaults(self) -> None:
        C = self._base_cfg

        # Pivots
        self.swing_lookback = int(C.get("swing_lookback", 5))
        self.min_swing_touches = int(C.get("min_swing_touches", 2))
        self.level_reach_atr = float(C.get("level_reach_atr", 2.0))
        self.watch_timeout_ms = int(C.get("watch_timeout_ms", 1_800_000))

        # Entry primary
        self.min_displacement_atr = float(C.get("min_displacement_atr", 0.7))
        self.min_delta_ratio = float(C.get("min_delta_ratio", 0.60))
        self.min_rvol = float(C.get("min_rvol", 1.5))
        self.min_rvol_reversal = float(C.get("min_rvol_reversal", 1.3))

        # Confirmations
        self.min_delta_efficiency = float(
            C.get("min_delta_efficiency", 1.0)
        )
        self.min_absorption_ratio = float(
            C.get("min_absorption_ratio", 5.0)
        )
        self.min_volume_z = float(C.get("min_volume_z", 1.0))

        # Stop composition
        self.b_break = float(C.get("b_break", 50.0))
        self.structural_buffer_mult = float(
            C.get("structural_buffer_mult", 1.5)
        )
        self.min_breath_atr = float(C.get("min_breath_atr", 1.5))
        self.max_risk_atr = float(C.get("max_risk_atr", 2.5))

        # Target
        self.target_min_atr = float(C.get("target_min_atr", 1.0))
        self.target_max_atr = float(C.get("target_max_atr", 3.0))
        self.d_major = float(C.get("d_major", 500.0))

        # Breakeven
        self.be_trigger_atr = float(C.get("be_trigger_atr", 1.0))
        self.be_buffer_atr = float(C.get("be_buffer_atr", 0.1))

        # Exit
        self.t_max_ms = int(C.get("t_max_ms", 1_800_000))
        self.enable_exhaustion_exit = bool(
            C.get("enable_exhaustion_exit", True)
        )
        self.exit_persistence_bars = int(
            C.get("exit_persistence_bars", 2)
        )

        # ATR
        self.atr_period = int(C.get("atr_period", 14))

    def _apply_params(self, symbol: str) -> None:
        P = lambda n, d: self._param(n, symbol, d)
        self.swing_lookback = int(P("swing_lookback", 5))
        self.min_swing_touches = int(P("min_swing_touches", 2))
        self.level_reach_atr = float(P("level_reach_atr", 2.0))
        self.watch_timeout_ms = int(P("watch_timeout_ms", 1_800_000))
        self.min_displacement_atr = float(P("min_displacement_atr", 0.7))
        self.min_delta_ratio = float(P("min_delta_ratio", 0.60))
        self.min_rvol = float(P("min_rvol", 1.5))
        self.min_rvol_reversal = float(P("min_rvol_reversal", 1.3))
        self.min_delta_efficiency = float(P("min_delta_efficiency", 1.0))
        self.min_absorption_ratio = float(P("min_absorption_ratio", 5.0))
        self.min_volume_z = float(P("min_volume_z", 1.0))
        self.b_break = float(P("b_break", 50.0))
        self.structural_buffer_mult = float(P("structural_buffer_mult", 1.5))
        self.min_breath_atr = float(P("min_breath_atr", 1.5))
        self.max_risk_atr = float(P("max_risk_atr", 2.5))
        self.target_min_atr = float(P("target_min_atr", 1.0))
        self.target_max_atr = float(P("target_max_atr", 3.0))
        self.d_major = float(P("d_major", 500.0))
        self.be_trigger_atr = float(P("be_trigger_atr", 1.0))
        self.be_buffer_atr = float(P("be_buffer_atr", 0.1))
        self.t_max_ms = int(P("t_max_ms", 1_800_000))
        self.enable_exhaustion_exit = bool(P("enable_exhaustion_exit", True))
        self.exit_persistence_bars = int(P("exit_persistence_bars", 2))
        self.atr_period = int(P("atr_period", 14))

    # ─── Lifecycle ─────────────────────────────────────────

    def dispose(self) -> None:
        for topic, handler in (
            (self._topic_feed_trade, self._on_trade),
            (self._topic_position, self._on_position),
            (self._topic_signal_failed, self._on_signal_failed),
        ):
            try:
                bus.unsubscribe(topic, handler)
            except Exception:
                pass
        log.info("FourPattern disposed")

    def set_symbol(self, symbol: str) -> None:
        already = getattr(self, "_active_symbol", None) == symbol
        self._active_symbol = symbol
        self._apply_params(symbol)
        if already:
            return
        self._reset_full()
        self._klines_1m[symbol] = deque(maxlen=500)
        self._klines_5m[symbol] = deque(maxlen=200)
        self._delta_5m[symbol] = deque(maxlen=100)
        self._trades[symbol] = deque(maxlen=20000)
        self._open_5m.pop(symbol, None)
        self._swings_cache.pop(symbol, None)
        if not hasattr(self, "_ticker_started"):
            self._ticker_started = True
            asyncio.create_task(self._state_ticker_loop())
        log.info(
            "FourPattern symbol → %s | lb=%d touches=%d reach=%.1fATR "
            "disp=%.2f rvol=%.1fx delta=%.2f breath=%.1fATR maxR=%.1fATR "
            "BE=%.1fATR tmax=%ds",
            symbol, self.swing_lookback, self.min_swing_touches,
            self.level_reach_atr, self.min_displacement_atr,
            self.min_rvol, self.min_delta_ratio,
            self.min_breath_atr, self.max_risk_atr,
            self.be_trigger_atr, self.t_max_ms // 1000,
        )

    def set_timeframe(self, timeframe: str) -> None:
        # Timeframe is strategy-internal (5m entry, 1m exit), ignore user.
        if timeframe not in ("5m", "1m"):
            log.info(
                "FourPattern ignores user timeframe=%r (uses 5m entry, 1m exit)",
                timeframe,
            )

    def warmup_timeframes(self, timeframe: str) -> list[str]:
        """Intervals the manager must seed before START.

        The entry engine needs 5m bars (pivots, ATR, regime) and the exit
        engine needs 1m bars. Seeding only the UI timeframe leaves ATR at
        zero, which disables every setup.
        """
        return ["5m", "1m"]

    def reset(self) -> None:
        self._reset_full()

    def reset_position_state(self) -> None:
        if self._state in ("IN_TRADE", "CLOSING") or self._entry_pending:
            return

    def reset_symbol(self, symbol: str) -> None:
        self._klines_1m.pop(symbol, None)
        self._klines_5m.pop(symbol, None)
        self._delta_5m.pop(symbol, None)
        self._trades.pop(symbol, None)
        self._open_5m.pop(symbol, None)
        self._swings_cache.pop(symbol, None)
        self._last_entry_eval_ts.pop(symbol, None)

    def has_open_position(self) -> bool:
        return (
            self._state in ("IN_TRADE", "CLOSING")
            or self._entry_pending
        )

    def _reset_full(self) -> None:
        self._state = "IDLE"
        self._state_ts = _now_ms()
        self._active_pivot = None
        self._trade = None
        self._entry_pending = False
        self._pending_entry_ts = 0

    # ─── Feed handlers ─────────────────────────────────────

    async def on_book(self, symbol, bids, asks):
        # Book is not used for entry/exit in this design.
        return None

    async def on_kline(
        self, symbol: str, payload: dict,
    ) -> Signal | None:
        """Single entry point for klines (live stream AND warmup).

        StrategyManager feeds warmup history through here on START and
        symbol switch, then live bars as they close. Exits are emitted
        from the bus in `_evaluate_exit` instead, so this always returns
        None.
        """
        if not symbol or symbol != getattr(self, "_active_symbol", None):
            return None
        interval = payload.get("interval")
        k = payload.get("kline")
        if not k or not k.get("closed"):
            return None
        try:
            bar = Bar(
                t=int(k["t"]),
                o=float(k["o"]),
                h=float(k["h"]),
                l=float(k["l"]),
                c=float(k["c"]),
                v=float(k["v"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if interval == "5m":
            await self._handle_5m_bar(symbol, bar)
        elif interval == "1m":
            await self._handle_1m_bar(symbol, bar)
        return None

    async def _handle_5m_bar(self, symbol: str, bar: Bar) -> None:
        buf = self._klines_5m.setdefault(symbol, deque(maxlen=200))
        if buf and buf[-1].t == bar.t:
            return  # dedupe (feed + aggregated)
        buf.append(bar)

        trades = self._trades.get(symbol, deque())
        _, _, delta = compute_delta(trades, bar.t, bar.t + _FIVE_MIN_MS)
        self._delta_5m.setdefault(symbol, deque(maxlen=100)).append(delta)

        if self._state == "WATCH":
            await self._evaluate_entry(symbol)

    async def _handle_1m_bar(self, symbol: str, bar: Bar) -> None:
        self._klines_1m.setdefault(symbol, deque(maxlen=500)).append(bar)
        await self._maybe_aggregate_5m(symbol, bar)

        # Never evaluate an exit before the entry has actually filled:
        # doing so can emit CLOSE while the order is still in flight, the
        # executor answers "no position", and the trade context is wiped
        # while a real position lands unmanaged.
        if self._state == "IN_TRADE" and not self._entry_pending:
            await self._evaluate_exit(symbol)

    async def _maybe_aggregate_5m(self, symbol: str, bar_1m: Bar) -> None:
        bucket_5m = (bar_1m.t // _FIVE_MIN_MS) * _FIVE_MIN_MS
        current = self._open_5m.get(symbol)

        if current is None or current["start"] != bucket_5m:
            if current is not None:
                completed = Bar(
                    t=current["start"],
                    o=current["o"], h=current["h"],
                    l=current["l"], c=current["c"], v=current["v"],
                )
                await self._handle_5m_bar(symbol, completed)
            self._open_5m[symbol] = {
                "start": bucket_5m,
                "o": bar_1m.o, "h": bar_1m.h,
                "l": bar_1m.l, "c": bar_1m.c, "v": bar_1m.v,
            }
        else:
            current["h"] = max(current["h"], bar_1m.h)
            current["l"] = min(current["l"], bar_1m.l)
            current["c"] = bar_1m.c
            current["v"] += bar_1m.v

    async def _on_trade(self, _topic: str, payload: dict) -> None:
        sym = payload.get("symbol")
        if not sym or sym != getattr(self, "_active_symbol", None):
            return
        try:
            ts = int(payload["trade"].get("ts", _now_ms()))
            price = float(payload["trade"]["price"])
            qty = float(payload["trade"]["qty"])
            side = str(payload["trade"]["side"])
        except (KeyError, TypeError, ValueError):
            return
        signed = qty if side == "BUY" else -qty
        self._trades.setdefault(sym, deque(maxlen=20000)).append(
            (ts, price, signed)
        )

    async def _on_position(self, _topic: str, payload: dict) -> None:
        if payload.get("symbol") != getattr(self, "_active_symbol", None):
            return
        position = payload.get("position")

        if position is None:
            if self._entry_pending:
                log.warning("FOUR_PATTERN entry failed — no fill")
                self._entry_pending = False
                self._state = "IDLE"
                self._state_ts = _now_ms()
            elif self._state == "CLOSING":
                self._state = "IDLE"
                self._state_ts = _now_ms()
                self._trade = None
            return

        if self._entry_pending and self._trade is not None:
            self._entry_pending = False
            fill = float(position.get("entry", 0.0) or 0.0)
            if fill > 0:
                self._trade.entry_price = fill
            self._trade.entry_ts = _now_ms()
            self._state = "IN_TRADE"
            self._state_ts = _now_ms()

            # Fill-past-stop guard
            if self._trade.pivot is not None and fill > 0:
                is_long = self._trade.direction == "BUY"
                if is_long and fill <= self._trade.stop_price:
                    log.warning(
                        "FOUR_PATTERN fill past stop — closing "
                        "dir=%s fill=%.4f stop=%.4f",
                        self._trade.direction, fill, self._trade.stop_price,
                    )
                    sig = self._close(
                        self._active_symbol or "",
                        f"FILL_PAST_STOP {self._trade.stop_price:.4f}",
                        fill,
                    )
                    if sig:
                        await bus.publish(
                            config.protocol.topics["signal_proposed"],
                            sig.to_dict(),
                        )
                    return
                if not is_long and fill >= self._trade.stop_price:
                    log.warning(
                        "FOUR_PATTERN fill past stop — closing "
                        "dir=%s fill=%.4f stop=%.4f",
                        self._trade.direction, fill, self._trade.stop_price,
                    )
                    sig = self._close(
                        self._active_symbol or "",
                        f"FILL_PAST_STOP {self._trade.stop_price:.4f}",
                        fill,
                    )
                    if sig:
                        await bus.publish(
                            config.protocol.topics["signal_proposed"],
                            sig.to_dict(),
                        )
                    return

            log.info(
                "FOUR_PATTERN FILL %s %s @ %.4f | pivot=%.4f "
                "stop=%.4f target=%.4f",
                self._trade.direction, self._trade.pattern,
                self._trade.entry_price,
                self._trade.pivot.price if self._trade.pivot else 0.0,
                self._trade.stop_price, self._trade.target_price,
            )
        elif self._state == "CLOSING":
            log.warning("FOUR_PATTERN close rejected — reverting to IN_TRADE")
            self._state = "IN_TRADE"
            self._state_ts = _now_ms()

    async def _on_signal_failed(self, _topic: str, payload: dict) -> None:
        if payload.get("symbol") != getattr(self, "_active_symbol", None):
            return
        if self._entry_pending:
            log.warning("FOUR_PATTERN signal_failed — resetting entry")
            self._entry_pending = False
            self._state = "IDLE"
            self._state_ts = _now_ms()
            self._trade = None

    # ─── Pivot detection ──────────────────────────────────

    def _find_pivots(self, symbol: str) -> list[Pivot]:
        ks = list(self._klines_5m.get(symbol, []))
        n = self.swing_lookback
        if len(ks) < n * 2 + 5:
            return []
        last_ts = ks[-1].t
        cached = self._swings_cache.get(symbol)
        if cached and cached[0] == last_ts:
            return cached[1]

        atr = self._current_atr_5m(symbol)
        if atr <= 0:
            return []

        tolerance = 0.5 * atr
        raw: list[tuple[str, float, int]] = []
        for i in range(n, len(ks) - n):
            h = ks[i].h
            l = ks[i].l
            ts = ks[i].t
            is_high = all(
                h >= ks[j].h
                for j in range(i - n, i + n + 1) if j != i
            )
            is_low = all(
                l <= ks[j].l
                for j in range(i - n, i + n + 1) if j != i
            )
            if is_high:
                raw.append(("resistance", h, ts))
            if is_low:
                raw.append(("support", l, ts))

        clusters: dict[str, list[list[tuple[float, int]]]] = {
            "support": [], "resistance": [],
        }
        for side, price, ts in raw:
            placed = False
            for cl in clusters[side]:
                if abs(cl[0][0] - price) <= tolerance:
                    cl.append((price, ts))
                    placed = True
                    break
            if not placed:
                clusters[side].append([(price, ts)])

        pivots: list[Pivot] = []
        for side, groups in clusters.items():
            for group in groups:
                if len(group) < self.min_swing_touches:
                    continue
                avg = sum(g[0] for g in group) / len(group)
                pivots.append(Pivot(
                    price=avg,
                    side=side,
                    touches=len(group),
                    detected_ts=max(g[1] for g in group),
                ))

        self._swings_cache[symbol] = (last_ts, pivots)
        return pivots

    def _find_nearest_pivot(
        self, symbol: str, price: float,
    ) -> Pivot | None:
        pivots = self._find_pivots(symbol)
        if not pivots:
            return None
        atr = self._current_atr_5m(symbol)
        if atr <= 0:
            return None
        reach = self.level_reach_atr * atr
        best: Pivot | None = None
        best_dist = reach
        for p in pivots:
            d = abs(price - p.price)
            if d <= best_dist:
                best = p
                best_dist = d
        return best

    # ─── State machine (5m close: entry) ──────────────────

    async def _evaluate_entry(self, symbol: str) -> None:
        if self._state != "WATCH":
            return
        bars_5m = list(self._klines_5m.get(symbol, []))
        if len(bars_5m) < self.atr_period + 6:
            return
        bar = bars_5m[-1]
        last_eval = self._last_entry_eval_ts.get(symbol, 0)
        if bar.t <= last_eval:
            return
        self._last_entry_eval_ts[symbol] = bar.t

        if self._active_pivot is None:
            self._state = "IDLE"
            return

        atr = self._current_atr_5m(symbol)
        if atr <= 0:
            return

        # 1. Classify pattern
        pattern, direction = self._classify_pattern(symbol, bar, atr)
        if pattern is None:
            return

        # 2. Primary signal (breakout vs reversal primary)
        if not self._primary_signal_ok(
            symbol, bar, direction, atr, pattern, bars_5m,
        ):
            return

        # 3. Regime filter
        if not self._regime_ok(symbol, direction, atr, bars_5m):
            return

        # 4. Confirmations
        count, details = self._count_confirmations(
            symbol, bar, direction, atr, bars_5m,
        )
        is_reversal = pattern in ("SPRING", "FAILED_RALLY")
        if is_reversal:
            # Reversal needs at least one star confirmation
            if not (details["cvd_divergence"] or details["absorption"]):
                return
        if count < 2:
            return

        # 5. Vetoes
        if self._has_veto(symbol, bar, direction, atr, bars_5m, pattern):
            return

        # 6. Fire
        self._enter_trade(
            symbol, bar, pattern, direction, atr, details, bars_5m,
        )

    def _classify_pattern(
        self, symbol: str, bar: Bar, atr: float,
    ) -> tuple[str | None, str | None]:
        pivot = self._active_pivot
        if pivot is None:
            return None, None
        buffer = 0.10 * atr

        if pivot.side == "support":
            # FAILED_DEFENSE_DOWN: opened above, closed clearly below
            if bar.o >= pivot.price and bar.c < pivot.price - buffer:
                return "FAILED_DEFENSE_DOWN", "SELL"
            # SPRING: wick touched support, bullish close
            if (
                bar.l <= pivot.price
                and bar.c > pivot.price
                and bar.c > bar.o
            ):
                return "SPRING", "BUY"
        elif pivot.side == "resistance":
            # FAILED_DEFENSE_UP: opened below, closed clearly above
            if bar.o <= pivot.price and bar.c > pivot.price + buffer:
                return "FAILED_DEFENSE_UP", "BUY"
            # FAILED_RALLY: wick touched resistance, bearish close
            if (
                bar.h >= pivot.price
                and bar.c < pivot.price
                and bar.c < bar.o
            ):
                return "FAILED_RALLY", "SELL"
        return None, None

    def _primary_signal_ok(
        self, symbol: str, bar: Bar, direction: str, atr: float,
        pattern: str, bars_5m: list[Bar],
    ) -> bool:
        is_breakout = pattern in (
            "FAILED_DEFENSE_UP", "FAILED_DEFENSE_DOWN",
        )
        rvol = compute_rvol(bars_5m, lookback=20)

        if is_breakout:
            if compute_displacement_atr(bar, atr) < self.min_displacement_atr:
                return False
            if rvol < self.min_rvol:
                return False
            trades = self._trades.get(symbol, deque())
            buy_v, sell_v, delta = compute_delta(
                trades, bar.t, bar.t + _FIVE_MIN_MS,
            )
            total = buy_v + sell_v
            if total <= 0:
                return False
            delta_ratio = delta / total if direction == "BUY" else -delta / total
            if delta_ratio < self.min_delta_ratio:
                return False
            return True

        # Reversal primary: elevated participation at the pivot
        if rvol < self.min_rvol_reversal:
            return False
        return True

    def _regime_ok(
        self, symbol: str, direction: str, atr: float,
        bars_5m: list[Bar],
    ) -> bool:
        if len(bars_5m) < 6:
            return False
        deltas = list(self._delta_5m.get(symbol, deque()))[-6:]
        if len(deltas) < 2:
            return False
        cvd_slope = deltas[-1] - deltas[0]
        poc_mig = compute_poc_migration(bars_5m, lookback=60, window=6)
        vwap = compute_vwap(bars_5m, lookback=20)
        if vwap <= 0 or atr <= 0:
            return False
        vwap_dev = (bars_5m[-1].c - vwap) / atr

        if direction == "BUY":
            return (
                cvd_slope > 0
                and poc_mig > 0
                and vwap_dev > -0.5
            )
        return (
            cvd_slope < 0
            and poc_mig < 0
            and vwap_dev < 0.5
        )

    def _count_confirmations(
        self, symbol: str, bar: Bar, direction: str,
        atr: float, bars_5m: list[Bar],
    ) -> tuple[int, dict]:
        trades = self._trades.get(symbol, deque())
        details = {
            "cvd_confirms": False,
            "cvd_divergence": False,
            "rvol": False,
            "delta_efficiency": False,
            "absorption": False,
            "sweep": False,
            "follow_through": False,
        }

        # CVD slope (short window, 3 bars)
        short_deltas = list(self._delta_5m.get(symbol, deque()))[-3:]
        if len(short_deltas) >= 2:
            short_slope = short_deltas[-1] - short_deltas[0]
            if direction == "BUY":
                details["cvd_confirms"] = short_slope > 0
            else:
                details["cvd_confirms"] = short_slope < 0

        # CVD divergence (last 10 × 5m bars).
        # Entry confirmation wants the divergence that SUPPORTS the trade:
        # a long entry is supported by bullish divergence (absorption at
        # the lows), a short by bearish divergence at the highs.
        cvd_series = compute_cvd_series(
            trades, 10 * _FIVE_MIN_MS,
            bar.t + _FIVE_MIN_MS, buckets=10,
        )
        if len(cvd_series) >= 3:
            details["cvd_divergence"] = detect_cvd_divergence(
                bars_5m[-10:], cvd_series,
                "bullish" if direction == "BUY" else "bearish",
            )

        # RVOL
        rvol = compute_rvol(bars_5m, lookback=20)
        details["rvol"] = rvol >= self.min_rvol

        # Volume Z-score
        vz = compute_volume_z(bars_5m, lookback=20)
        details["delta_efficiency"] = False  # placeholder; set below

        # Delta efficiency
        buy_v, sell_v, delta = compute_delta(
            trades, bar.t, bar.t + _FIVE_MIN_MS,
        )
        eff = compute_delta_efficiency(bar.o, bar.c, delta)
        details["delta_efficiency"] = eff >= self.min_delta_efficiency

        # Absorption (reversal signal)
        absorb = compute_absorption_ratio(bar, atr)
        details["absorption"] = absorb >= self.min_absorption_ratio

        # Sweep (only for reversals, requires pivot)
        if self._active_pivot is not None:
            details["sweep"] = detect_sweep(
                bar, self._active_pivot.price, direction, atr,
                min_wick_atr=0.5,
            )

        # Follow-through (prev + current bar both beyond pivot)
        if len(bars_5m) >= 2 and self._active_pivot is not None:
            details["follow_through"] = detect_follow_through(
                bars_5m[-2], bars_5m[-1],
                self._active_pivot.price, direction,
            )

        # Fold volume Z into confirmations (adds as extra confirmation)
        details["volume_z"] = vz >= self.min_volume_z

        count = sum(1 for k in (
            "cvd_confirms", "cvd_divergence", "rvol",
            "delta_efficiency", "absorption", "sweep",
            "follow_through", "volume_z",
        ) if details.get(k))
        return count, details

    def _has_veto(
        self, symbol: str, bar: Bar, direction: str,
        atr: float, bars_5m: list[Bar], pattern: str,
    ) -> bool:
        is_breakout = pattern in (
            "FAILED_DEFENSE_UP", "FAILED_DEFENSE_DOWN",
        )
        trades = self._trades.get(symbol, deque())
        deltas_series = list(self._delta_5m.get(symbol, deque()))[-3:]
        rvol = compute_rvol(bars_5m, lookback=20)
        disp = compute_displacement_atr(bar, atr)

        # Veto: heavy volume + small displacement (absorption against breakout)
        if is_breakout and disp < 0.5 and rvol > 1.8:
            return True

        # Veto: CVD flat / fading on breakout
        if is_breakout and len(deltas_series) >= 2:
            if direction == "BUY" and deltas_series[-1] <= 0:
                return True
            if direction == "SELL" and deltas_series[-1] >= 0:
                return True

        # Veto: wick closed back inside the pivot
        if self._active_pivot is not None:
            p = self._active_pivot.price
            if is_breakout:
                if direction == "BUY" and bar.h > p and bar.c < p:
                    return True
                if direction == "SELL" and bar.l < p and bar.c > p:
                    return True

        # Veto: low participation
        if rvol < 1.0:
            return True

        return False

    def _enter_trade(
        self, symbol: str, bar: Bar, pattern: str,
        direction: str, atr: float, details: dict,
        bars_5m: list[Bar],
    ) -> None:
        pivot = self._active_pivot
        if pivot is None:
            self._state = "IDLE"
            return

        entry_price = bar.c
        is_long = direction == "BUY"

        # Stop = structural + breathing + cap
        if is_long:
            structural_level = pivot.price - (self.b_break * self.structural_buffer_mult)
            structural_dist = entry_price - structural_level
        else:
            structural_level = pivot.price + (self.b_break * self.structural_buffer_mult)
            structural_dist = structural_level - entry_price

        chosen_dist = max(
            self.min_breath_atr * atr,
            min(structural_dist, self.max_risk_atr * atr),
        )
        stop_price = (
            entry_price - chosen_dist if is_long
            else entry_price + chosen_dist
        )

        # Target = next pivot, ATR-bounded
        target_price = self._compute_target(
            symbol, entry_price, direction, atr, bars_5m,
        )

        self._trade = TradeContext(
            pattern=pattern,
            direction=direction,
            pivot=pivot,
            entry_price=entry_price,
            entry_ts=_now_ms(),
            stop_price=stop_price,
            target_price=target_price,
            original_stop=stop_price,
            atr_at_entry=atr,
            entry_confirmations=details,
        )
        self._entry_pending = True
        self._pending_entry_ts = _now_ms()
        self._state = "IN_TRADE"
        self._state_ts = _now_ms()

        log.info(
            "FOUR_PATTERN ENTER %s (%s) @ %.4f | pivot=%.4f "
            "stop=%.4f (%.2fATR) target=%.4f (%.2fATR) conf=%d",
            direction, pattern, entry_price, pivot.price,
            stop_price, chosen_dist / atr if atr > 0 else 0.0,
            target_price, abs(target_price - entry_price) / atr if atr > 0 else 0.0,
            sum(1 for v in details.values() if v is True),
        )

        sig = Signal(
            strategy=self.name,
            symbol=symbol,
            action=direction,
            confidence=0.85,
            reason=pattern,
            price=entry_price,
            metadata={
                "scenario": pattern,
                "size_multiplier": 1.0,
                "force_taker": True,
                "entry_plan": {
                    "setup": pattern,
                    "side": direction,
                    "entry_price": entry_price,
                    "entry_ts": self._trade.entry_ts,
                    "entry_atr": atr,
                    "stop_distance": chosen_dist,
                    "stop": stop_price,
                    "target": target_price,
                    "base_pivot": pivot.price,
                    "base_pivot_touches": pivot.touches,
                    "confirmations": details,
                },
            },
        )
        self._emit_debug(symbol, entry_price, sig_obj=sig)
        asyncio.create_task(
            bus.publish(
                config.protocol.topics["signal_proposed"], sig.to_dict(),
            )
        )

    def _compute_target(
        self, symbol: str, entry_price: float,
        direction: str, atr: float, bars_5m: list[Bar],
    ) -> float:
        is_long = direction == "BUY"
        pivot = self._active_pivot
        ref = pivot.price if pivot else entry_price

        candidates: list[float] = []
        for p in self._find_pivots(symbol):
            if is_long and p.side == "resistance" and p.price >= ref + self.d_major:
                candidates.append(p.price)
            elif not is_long and p.side == "support" and p.price <= ref - self.d_major:
                candidates.append(p.price)

        if candidates:
            raw_target = min(candidates) if is_long else max(candidates)
        else:
            raw_target = (
                entry_price + self.target_min_atr * atr if is_long
                else entry_price - self.target_min_atr * atr
            )

        # Clamp to [min_atr, max_atr]
        dist = abs(raw_target - entry_price)
        dist = max(dist, self.target_min_atr * atr)
        dist = min(dist, self.target_max_atr * atr)
        return entry_price + dist if is_long else entry_price - dist

    # ─── Exit (1m close) ──────────────────────────────────

    async def _evaluate_exit(self, symbol: str) -> None:
        if self._state != "IN_TRADE" or self._trade is None:
            return
        bars_1m = list(self._klines_1m.get(symbol, []))
        if not bars_1m:
            return
        bar = bars_1m[-1]
        t = self._trade
        price = bar.c
        atr = t.atr_at_entry
        if atr <= 0:
            return
        age_ms = _now_ms() - t.entry_ts
        is_long = t.direction == "BUY"

        if is_long:
            t.mfe = max(t.mfe, price)
            t.mae = min(t.mae, price)
        else:
            t.mfe = min(t.mfe, price)
            t.mae = max(t.mae, price)

        # Layer 1: hard stop
        if is_long and price <= t.stop_price:
            sig = self._close(symbol, f"STOP {t.stop_price:.4f}", price)
            if sig:
                await bus.publish(
                    config.protocol.topics["signal_proposed"],
                    sig.to_dict(),
                )
            return
        if not is_long and price >= t.stop_price:
            sig = self._close(symbol, f"STOP {t.stop_price:.4f}", price)
            if sig:
                await bus.publish(
                    config.protocol.topics["signal_proposed"],
                    sig.to_dict(),
                )
            return

        # Layer 2: target
        if is_long and price >= t.target_price:
            sig = self._close(symbol, f"TARGET {t.target_price:.4f}", price)
            if sig:
                await bus.publish(
                    config.protocol.topics["signal_proposed"],
                    sig.to_dict(),
                )
            return
        if not is_long and price <= t.target_price:
            sig = self._close(symbol, f"TARGET {t.target_price:.4f}", price)
            if sig:
                await bus.publish(
                    config.protocol.topics["signal_proposed"],
                    sig.to_dict(),
                )
            return

        # Layer 3: time stop
        if age_ms > self.t_max_ms:
            sig = self._close(
                symbol, f"TIME_STOP {age_ms // 1000}s", price,
            )
            if sig:
                await bus.publish(
                    config.protocol.topics["signal_proposed"],
                    sig.to_dict(),
                )
            return

        # Layer 4: breakeven shift
        if not t.be_triggered:
            profit = (price - t.entry_price) if is_long else (t.entry_price - price)
            if profit >= self.be_trigger_atr * atr:
                offset = self.be_buffer_atr * atr
                t.stop_price = t.entry_price + offset if is_long else t.entry_price - offset
                t.be_triggered = True
                log.info(
                    "FOUR_PATTERN BE @ %.4f | stop=%.4f",
                    t.entry_price, t.stop_price,
                )
                # Broker-side protection must follow the in-memory stop,
                # otherwise a crash reverts us to the original wide stop.
                await self._publish_stop_sync(symbol, t.stop_price, "breakeven")

        # Layer 5: exhaustion exit (only after BE)
        if self.enable_exhaustion_exit and t.be_triggered:
            if self._regime_turned(symbol, t.direction, atr):
                conf_count, _ = self._count_exit_confirmations(
                    symbol, bar, t.direction, atr, bars_1m,
                )
                if conf_count >= 2:
                    t.exit_signal_count += 1
                    if t.exit_signal_count >= self.exit_persistence_bars:
                        sig = self._close(
                            symbol,
                            f"EXHAUSTION conf={conf_count}",
                            price,
                        )
                        if sig:
                            await bus.publish(
                                config.protocol.topics["signal_proposed"],
                                sig.to_dict(),
                            )
                        return
                else:
                    t.exit_signal_count = 0

    def _regime_turned(
        self, symbol: str, direction: str, atr: float,
    ) -> bool:
        bars_5m = list(self._klines_5m.get(symbol, []))
        deltas = list(self._delta_5m.get(symbol, deque()))
        if len(bars_5m) < 6 or len(deltas) < 8:
            return False

        if direction == "BUY":
            # CVD turned: last 2 deltas negative after being net positive
            cvd_turned = (
                deltas[-1] < 0
                and deltas[-2] < 0
                and sum(deltas[-6:-2]) > 0
            )
            if not cvd_turned:
                return False
            poc_mig = compute_poc_migration(bars_5m, lookback=60, window=6)
            vwap = compute_vwap(bars_5m, lookback=20)
            vwap_dev = (bars_5m[-1].c - vwap) / atr if vwap > 0 else 0.0
            return poc_mig < -0.5 * atr or vwap_dev < -0.3
        else:
            cvd_turned = (
                deltas[-1] > 0
                and deltas[-2] > 0
                and sum(deltas[-6:-2]) < 0
            )
            if not cvd_turned:
                return False
            poc_mig = compute_poc_migration(bars_5m, lookback=60, window=6)
            vwap = compute_vwap(bars_5m, lookback=20)
            vwap_dev = (bars_5m[-1].c - vwap) / atr if vwap > 0 else 0.0
            return poc_mig > 0.5 * atr or vwap_dev > 0.3

    def _count_exit_confirmations(
        self, symbol: str, bar: Bar, direction: str,
        atr: float, bars_1m: list[Bar],
    ) -> tuple[int, dict]:
        trades = self._trades.get(symbol, deque())
        details = {
            "absorption": False,
            "delta_exhaustion": False,
            "cvd_divergence": False,
            "follow_through_failure": False,
        }

        # Absorption against trade direction
        absorb = compute_absorption_ratio(bar, atr)
        if absorb >= self.min_absorption_ratio:
            details["absorption"] = True

        # Delta exhaustion: 3 consecutive declining (for long) deltas
        if len(bars_1m) >= 3:
            deltas = []
            for b in bars_1m[-3:]:
                _, _, d = compute_delta(trades, b.t, b.t + _ONE_MIN_MS)
                deltas.append(d)
            if direction == "BUY":
                details["delta_exhaustion"] = (
                    deltas[-1] < deltas[-2] < deltas[-3]
                )
            else:
                details["delta_exhaustion"] = (
                    deltas[-1] > deltas[-2] > deltas[-3]
                )

        # CVD divergence over last 6 × 1m.
        # An exhaustion exit wants the divergence OPPOSING the open trade:
        # a long is exhausted by bearish divergence at the highs.
        cvd_series = compute_cvd_series(
            trades, 6 * _ONE_MIN_MS,
            bar.t + _ONE_MIN_MS, buckets=6,
        )
        if len(cvd_series) >= 3:
            details["cvd_divergence"] = detect_cvd_divergence(
                bars_1m[-6:], cvd_series,
                "bearish" if direction == "BUY" else "bullish",
            )

        # Follow-through failure: returned to pivot
        if self._trade and self._trade.pivot:
            p = self._trade.pivot.price
            if direction == "BUY" and bar.c <= p:
                details["follow_through_failure"] = True
            if direction == "SELL" and bar.c >= p:
                details["follow_through_failure"] = True

        count = sum(1 for v in details.values() if v)
        return count, details

    async def _publish_stop_sync(
        self, symbol: str, stop: float, reason: str,
    ) -> None:
        try:
            await bus.publish(
                config.protocol.topics.get("cmd_sync_stop", "cmd_sync_stop"),
                {"symbol": symbol, "stop": float(stop), "reason": reason},
            )
        except Exception:
            log.exception("Failed to publish stop sync for %s", symbol)

    def _close(
        self, symbol: str, reason: str, exit_price: float,
    ) -> Signal | None:
        if self._state != "IN_TRADE" or self._trade is None:
            return None
        t = self._trade
        log.info(
            "FOUR_PATTERN EXIT %s %s %s @ %.4f | entry=%.4f pnl=%.2fATR",
            t.direction, symbol, reason, exit_price, t.entry_price,
            (exit_price - t.entry_price) / t.atr_at_entry
            if t.direction == "BUY" and t.atr_at_entry > 0
            else (t.entry_price - exit_price) / t.atr_at_entry
            if t.atr_at_entry > 0 else 0.0,
        )
        sig = Signal(
            strategy=self.name,
            symbol=symbol,
            action="CLOSE",
            confidence=0.9,
            reason=reason,
            price=exit_price,
            metadata={
                "exit": reason,
                "pattern": t.pattern,
                "entry_price": t.entry_price,
                "exit_price": exit_price,
                "pivot": t.pivot.price if t.pivot else 0.0,
                "target": t.target_price,
                "mfe": t.mfe,
                "mae": t.mae,
                "be_triggered": t.be_triggered,
            },
        )
        self._state = "CLOSING"
        self._state_ts = _now_ms()
        self._emit_debug(symbol, exit_price, reason=reason)
        return sig

    # ─── Entry-pending timeout watchdog ──────────────────

    async def _entry_watchdog(self) -> None:
        """Called from state machine occasionally. Not a loop."""
        if (
            self._entry_pending
            and _now_ms() - self._pending_entry_ts > 15_000
        ):
            log.warning("FOUR_PATTERN entry timeout")
            self._entry_pending = False
            self._state = "IDLE"
            self._state_ts = _now_ms()
            self._trade = None

    # ─── ATR ──────────────────────────────────────────────

    def _current_atr_5m(self, symbol: str) -> float:
        bars = list(self._klines_5m.get(symbol, []))
        if len(bars) < self.atr_period + 1:
            return 0.0
        return compute_atr(bars, self.atr_period)

    # ─── State transitions ────────────────────────────────

    def _enter_watch(self, symbol: str) -> None:
        bars = list(self._klines_5m.get(symbol, []))
        if not bars:
            return
        price = bars[-1].c
        pivot = self._find_nearest_pivot(symbol, price)
        if pivot is None:
            return
        self._active_pivot = pivot
        self._state = "WATCH"
        self._state_ts = _now_ms()
        log.info(
            "FOUR_PATTERN IDLE → WATCH (pivot:%s:%.4f:t%d)",
            pivot.side, pivot.price, pivot.touches,
        )

    # ─── Called by manager on START ──────────────────────

    async def _check_state_progression(self, symbol: str) -> None:
        """If IDLE, look for pivot; if WATCH, time out; if entry pending, timeout."""
        if self._entry_pending:
            if _now_ms() - self._pending_entry_ts > 15_000:
                log.warning("FOUR_PATTERN entry timeout")
                self._entry_pending = False
                self._state = "IDLE"
                self._state_ts = _now_ms()
                self._trade = None
            return

        if self._state == "IDLE":
            self._enter_watch(symbol)
            return

        if self._state == "WATCH":
            if _now_ms() - self._state_ts > self.watch_timeout_ms:
                log.info("FOUR_PATTERN WATCH → IDLE (watch_timeout)")
                self._state = "IDLE"
                self._state_ts = _now_ms()
                self._active_pivot = None
                return
            # Price moved too far from pivot
            if self._active_pivot is not None:
                bars = list(self._klines_5m.get(symbol, []))
                if bars:
                    price = bars[-1].c
                    atr = self._current_atr_5m(symbol)
                    if atr > 0 and abs(price - self._active_pivot.price) > self.level_reach_atr * atr:
                        log.info(
                            "FOUR_PATTERN WATCH → IDLE (price_left_level)"
                        )
                        self._state = "IDLE"
                        self._state_ts = _now_ms()
                        self._active_pivot = None
            return

    # ─── State machine ticker ────────────────────────────

    async def _state_ticker_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5.0)
                sym = getattr(self, "_active_symbol", None)
                if sym:
                    try:
                        await self._check_state_progression(sym)
                    except Exception:
                        log.exception("FOUR_PATTERN ticker error")
        except asyncio.CancelledError:
            return

    # ─── Emit debug ──────────────────────────────────────

    def _emit_debug(
        self, symbol: str, mid: float,
        sig_obj: Signal | None = None, reason: str = "",
    ) -> None:
        decision = "WAIT"
        if sig_obj is not None:
            decision = "ENTER" if sig_obj.action != "CLOSE" else "EXIT"
        payload = {
            "strategy": self.name,
            "ts": _now_ms(),
            "symbol": symbol,
            "mid": mid,
            "state": self._state,
            "pattern": self._trade.pattern if self._trade else "",
            "decision": decision,
            "reason": reason or (
                sig_obj.reason if sig_obj is not None else ""
            ),
        }
        asyncio.create_task(bus.publish(self._topic_debug, payload))

    def dump_zones(self, symbol: str = "") -> dict[str, Any]:
        sym = symbol or getattr(self, "_active_symbol", "")
        pivots = self._find_pivots(sym)
        return {
            "strategy": self.name,
            "symbol": sym,
            "state": self._state,
            "active_pivot": {
                "price": self._active_pivot.price,
                "side": self._active_pivot.side,
                "touches": self._active_pivot.touches,
            } if self._active_pivot else None,
            "pivots_count": len(pivots),
            "trade": {
                "direction": self._trade.direction,
                "pattern": self._trade.pattern,
                "entry": self._trade.entry_price,
                "stop": self._trade.stop_price,
                "target": self._trade.target_price,
                "be_triggered": self._trade.be_triggered,
            } if self._trade else None,
        }