"""XAUUSD Four-Pattern Order-Flow Scalping Strategy.

Unified thesis: every trade is a failure trade. One side tries, fails, 
and the failure creates trapped fuel for the next move.

Execution: Pure zone-to-zone. Blind MT5 (BUY / SELL / CLOSE only). 
No broker-side SL, no broker-side TP. All exits are evaluated 
synthetically on the spot feed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.core.config import config
from app.core.event_bus import bus
from app.modules.strategy.base import Signal, Strategy

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


class State(str, Enum):
    IDLE = "IDLE"
    WATCH = "WATCH"
    TEST_PHASE = "TEST_PHASE"
    REVERSAL_PENDING = "REVERSAL_PENDING"
    FLIP_CONFIRMED = "FLIP_CONFIRMED"
    BREAKOUT_PENDING = "BREAKOUT_PENDING"
    COUNTER_ATTACK = "COUNTER_ATTACK"
    ABSORBED = "ABSORBED"
    ARMED = "ARMED"
    IN_TRADE = "IN_TRADE"
    CLOSED = "CLOSED"


@dataclass
class Level:
    price: float
    side: str          # "support" | "resistance"
    touches: int = 0
    depth: float = 0.0
    detected_ts: int = 0
    invalidation_low: float = 0.0
    invalidation_high: float = 0.0


@dataclass
class Test:
    side: str          # "BUY" (buyer-aggressive) | "SELL" (seller-aggressive)
    volume: float
    price: float
    ts: int


@dataclass
class TradeContext:
    pattern: str = ""
    direction: str = ""
    level: Level | None = None
    entry_price: float = 0.0
    entry_ts: int = 0
    stop_target: float | None = None  # The 'hot' upper/lower zone
    next_wall: float | None = None    # The backstop zone
    mfe: float = 0.0
    mae: float = 0.0
    db_row_id: int = 0
    
    # ─── §12 Journal Metrics ───
    test_volume: float = 0.0
    flip_volume: float = 0.0
    absorption_depth: float = 0.0
    outcome: str = ""
    level_side: str = ""


class FourPattern(Strategy):
    name = "four_pattern"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)

        # ─── Parameters (§11) ──────────────────────────────
        self.k_wall = float(cfg.get("k_wall", 4.0))
        self.t_wall_ms = int(cfg.get("t_wall_ms", 1000))
        self.d_min_wall = float(cfg.get("d_min_wall", 1.00))
        self.k_test = float(cfg.get("k_test", 2.0))
        self.d_test_atr = float(cfg.get("d_test_atr", 0.3))
        self.w_outcome_ms = int(cfg.get("w_outcome_ms", 3000))
        self.b_break = float(cfg.get("b_break", 0.20))
        self.k_flip = float(cfg.get("k_flip", 1.5))
        self.d_at_level_atr = float(cfg.get("d_at_level_atr", 0.3))
        self.w_flip_ms = int(cfg.get("w_flip_ms", 5000))
        self.w_counter_ms = int(cfg.get("w_counter_ms", 3000))
        self.k_counter = float(cfg.get("k_counter", 2.0))
        self.b_hold = float(cfg.get("b_hold", 0.15))
        self.k_absorb = float(cfg.get("k_absorb", 1.5))
        self.d_max_drop = float(cfg.get("d_max_drop", 0.40))
        self.d_major = float(cfg.get("d_major", 5.00))
        self.d_stop = float(cfg.get("d_stop", 3.00))
        self.w_trig_ms = int(cfg.get("w_trig_ms", 1500))
        self.tape_spike_mult = float(cfg.get("tape_spike_mult", 3.0))
        self.delta_one_sided = float(cfg.get("delta_one_sided", 0.65))
        self.t_entry_ms = int(cfg.get("t_entry_ms", 500))
        self.b_exit = float(cfg.get("b_exit", 0.30))
        self.b_wall = float(cfg.get("b_wall", 0.30))
        self.d_min_profit = float(cfg.get("d_min_profit", 1.00))
        self.cost_buffer = float(cfg.get("cost_buffer", 0.60))
        self.t_max_ms = int(cfg.get("t_max_ms", 120000))
        self.s_max_spread = float(cfg.get("s_max_spread", 0.50))
        self.atr_period = int(cfg.get("atr_period", 14))
        self.watch_timeout_ms = int(cfg.get("watch_timeout_ms", 120000))
        self.reassess_timeout_ms = int(cfg.get("reassess_timeout_ms", 30000))

        # Session filter
        self.session_start_hour_utc = int(cfg.get("session_start_hour_utc", 7))
        self.session_end_hour_utc = int(cfg.get("session_end_hour_utc", 20))

        # Pattern weights (§8)
        self.weights = {
            "SPRING": 1.5,
            "FAILED_RALLY": 1.5,
            "FAILED_DEFENSE_UP": 1.0,
            "FAILED_DEFENSE_DOWN": 1.0,
        }

        # ─── Runtime state ─────────────────────────────────
        self._state = State.IDLE
        self._state_ts = _now_ms()
        self._active_level: Level | None = None
        self._active_test: Test | None = None
        self._outcome_observed = ""
        self._break_price = 0.0
        self._flip_volume = 0.0
        self._counter_volume = 0.0
        self._absorption_depth = 0.0
        self._absorption_baseline = 0.0
        self._armed_direction = ""
        self._armed_pattern = ""
        self._armed_ts = 0
        self._last_entry_pattern = ""
        self._trade = TradeContext()
        self._entry_pending = False
        self._pending_entry_ts = 0

        # ─── Market data ──────────────────────────────────
        self._klines: dict[str, deque] = {}
        self._atr_history: dict[str, deque] = {}
        self._trades: dict[str, deque] = {}
        self._book_bids: dict[str, dict[float, float]] = {}
        self._book_asks: dict[str, dict[float, float]] = {}
        self._per_second_vol: dict[str, deque] = {}

        # ─── Subscriptions ────────────────────────────────
        self._topic_feed_trade = config.protocol.topics.get("feed_trade", "feed.trade")
        self._topic_position = config.protocol.topics.get("position_update", "position.update")
        self._topic_debug = config.protocol.topics.get("debug_decision", "debug.decision")

        bus.subscribe(self._topic_feed_trade, self._on_trade)
        bus.subscribe(self._topic_position, self._on_position)

    # ═══════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════

    def dispose(self) -> None:
        for topic, handler in (
            (self._topic_feed_trade, self._on_trade),
            (self._topic_position, self._on_position),
        ):
            try:
                bus.unsubscribe(topic, handler)
            except Exception:
                pass
        log.info("FourPattern disposed")

    def set_symbol(self, symbol: str) -> None:
        if getattr(self, "_active_symbol", None) == symbol:
            return
        self._active_symbol = symbol
        self._reset_full()
        self._klines.setdefault(symbol, deque(maxlen=200))
        self._trades.setdefault(symbol, deque(maxlen=20000))
        self._per_second_vol.setdefault(symbol, deque(maxlen=300))

    def set_timeframe(self, timeframe: str) -> None:
        self._timeframe = timeframe

    def reset(self) -> None:
        self._reset_full()

    def reset_position_state(self) -> None:
        if self._state == State.IN_TRADE or self._entry_pending:
            return

    def reset_symbol(self, symbol: str) -> None:
        self._klines.pop(symbol, None)
        self._atr_history.pop(symbol, None)
        self._trades.pop(symbol, None)
        self._per_second_vol.pop(symbol, None)
        self._book_bids.pop(symbol, None)
        self._book_asks.pop(symbol, None)

    def _reset_full(self) -> None:
        self._state = State.IDLE
        self._state_ts = _now_ms()
        self._active_level = None
        self._active_test = None
        self._outcome_observed = ""
        self._break_price = 0.0
        self._flip_volume = 0.0
        self._counter_volume = 0.0
        self._absorption_depth = 0.0
        self._absorption_baseline = 0.0
        self._armed_direction = ""
        self._armed_pattern = ""
        self._armed_ts = 0
        self._trade = TradeContext()
        self._entry_pending = False

    # ═══════════════════════════════════════════════════════
    # Data ingestion
    # ═══════════════════════════════════════════════════════

    async def on_book(self, symbol: str, bids: list, asks: list) -> Signal | None:
        if not bids or not asks or symbol != getattr(self, "_active_symbol", None):
            return None
        self._book_bids[symbol] = {float(p): float(q) for p, q in bids[:30]}
        self._book_asks[symbol] = {float(p): float(q) for p, q in asks[:30]}

        if self._state == State.IN_TRADE:
            mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
            return await self._evaluate_exit(symbol, mid, _now_ms())
        return None

    async def on_kline(self, symbol: str, payload: dict) -> Signal | None:
        if payload.get("interval") != getattr(self, "_timeframe", "1m"):
            return None
        k = payload.get("kline")
        if not k or not k.get("closed"):
            return None

        candle = {
            "t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
            "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"]),
        }
        buf = self._klines.setdefault(symbol, deque(maxlen=200))
        buf.append(candle)
        self._update_atr(symbol)

        if self._state in (State.IDLE, State.CLOSED):
            self._try_detect_level(symbol, candle["c"])
            if self._state == State.CLOSED and _now_ms() - self._state_ts > self.reassess_timeout_ms:
                self._state = State.IDLE
                self._state_ts = _now_ms()

        return None

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

        signed_qty = qty if side == "BUY" else -qty
        buf = self._trades.setdefault(sym, deque(maxlen=20000))
        buf.append((ts, price, signed_qty))

        self._update_per_second_volume(sym, ts, signed_qty, side)

        if self._state == State.IN_TRADE:
            sig = await self._evaluate_exit(sym, price, _now_ms())
            if sig:
                await bus.publish(config.protocol.topics["signal_proposed"], sig.to_dict())
            return

        if self._entry_pending:
            return

        now = _now_ms()
        await self._dispatch_state_machine(sym, price, now)

    async def _on_position(self, _topic: str, payload: dict) -> None:
        if payload.get("symbol") != getattr(self, "_active_symbol", None):
            return
        position = payload.get("position")

        if position is None:
            if self._entry_pending:
                log.warning("Entry failed - position not filled")
                self._entry_pending = False
                self._state = State.IDLE
                self._state_ts = _now_ms()
            return

        if self._entry_pending:
            self._entry_pending = False
            fill = float(position.get("entry", 0.0) or 0.0)
            self._trade.entry_price = fill if fill > 0 else self._trade.entry_price
            self._trade.entry_ts = _now_ms()
            self._state = State.IN_TRADE
            self._state_ts = _now_ms()
            log.info(
                f"FOUR_PATTERN FILL {self._trade.direction} {self._trade.pattern} "
                f"@ {self._trade.entry_price:.2f} (Naked Entry)"
            )

    # ═══════════════════════════════════════════════════════
    # State Machine
    # ═══════════════════════════════════════════════════════

    async def _dispatch_state_machine(self, symbol: str, price: float, now: int) -> None:
        if not self._session_ok(now):
            if self._state != State.IDLE:
                self._transition(State.IDLE, "session_filter")
            return

        if not self._spread_ok(symbol):
            if self._state != State.IDLE:
                self._transition(State.IDLE, "spread_too_wide")
            return

        if self._state == State.IDLE:
            return

        elif self._state == State.WATCH:
            if now - self._state_ts > self.watch_timeout_ms:
                self._transition(State.IDLE, "watch_timeout")
                return
            test = self._detect_aggressive_test(symbol, price, now)
            if test:
                self._active_test = test
                self._transition(State.TEST_PHASE, f"test:{test.side}:{test.volume:.1f}")

        elif self._state == State.TEST_PHASE:
            self._check_outcome(symbol, price, now)

        elif self._state == State.REVERSAL_PENDING:
            if now - self._state_ts > self.w_flip_ms:
                self._transition(State.IDLE, "no_flip")
                return
            if self._detect_flip(symbol, price, now):
                self._transition(State.FLIP_CONFIRMED, f"flip:{self._flip_volume:.1f}")

        elif self._state == State.FLIP_CONFIRMED:
            self._evaluate_armed(symbol, price, now, branch="reversal")

        elif self._state == State.BREAKOUT_PENDING:
            if now - self._state_ts > self.w_counter_ms:
                self._transition(State.IDLE, "vacuum_break")
                return
            if self._detect_counter_attack(symbol, price, now):
                self._transition(State.COUNTER_ATTACK, f"counter:{self._counter_volume:.1f}")

        elif self._state == State.COUNTER_ATTACK:
            if self._check_absorption(symbol, price, now):
                self._transition(State.ABSORBED, "absorbed")
            elif self._counter_succeeded(symbol, price):
                self._transition(State.IDLE, "counter_succeeded")

        elif self._state == State.ABSORBED:
            self._evaluate_armed(symbol, price, now, branch="breakout")

        elif self._state == State.ARMED:
            if now - self._armed_ts > self.t_entry_ms:
                self._transition(State.IDLE, "armed_stale")
                return
            if self._tape_spike_confirmed(symbol, price, now):
                self._enter_trade(symbol, price, now)

    def _transition(self, new_state: State, reason: str = "") -> None:
        if self._state != new_state:
            log.info(f"FOUR_PATTERN {self._state.value} → {new_state.value} ({reason})")
        self._state = new_state
        self._state_ts = _now_ms()
        self._emit_debug(self._active_symbol or "", 0.0, reason=reason)

    # ═══════════════════════════════════════════════════════
    # Level detection (§3)
    # ═══════════════════════════════════════════════════════

    def _try_detect_level(self, symbol: str, current_price: float) -> None:
        ks = self._klines.get(symbol)
        if not ks or len(ks) < 20:
            return
        atr = self._current_atr(symbol)
        if atr <= 0:
            return

        recent = list(ks)[-20:]
        closes = [c["c"] for c in recent]
        tolerance = 0.5 * atr

        clusters: list[list[float]] = []
        for c in closes:
            placed = False
            for cl in clusters:
                if abs(c - cl[0]) <= tolerance:
                    cl.append(c)
                    placed = True
                    break
            if not placed:
                clusters.append([c])

        for cl in clusters:
            if len(cl) < 3:
                continue
            lvl_price = sum(cl) / len(cl)

            if abs(lvl_price - current_price) > 5 * atr:
                continue

            side = "support" if current_price >= lvl_price else "resistance"

            if not self._depth_sufficient(symbol, lvl_price, side, atr):
                continue

            lvl = Level(
                price=lvl_price,
                side=side,
                touches=len(cl),
                detected_ts=_now_ms(),
                invalidation_low=lvl_price - 0.5 * atr,
                invalidation_high=lvl_price + 0.5 * atr,
            )
            self._active_level = lvl
            self._transition(State.WATCH, f"level:{side}:{lvl_price:.2f}:t{lvl.touches}")
            return

    def _depth_sufficient(self, symbol: str, price: float, side: str, atr: float) -> bool:
        book = self._book_bids.get(symbol, {}) if side == "support" else self._book_asks.get(symbol, {})
        if not book:
            return True
        depths = sorted(book.values())
        if not depths:
            return True
        median_depth = depths[len(depths) // 2]
        total = sum(
            q for p, q in book.items() if abs(p - price) <= 0.5 * atr
        )
        return total >= 2.0 * median_depth

    # ═══════════════════════════════════════════════════════
    # Test / Outcome / Flip / Counter (§3)
    # ═══════════════════════════════════════════════════════

    def _detect_aggressive_test(self, symbol: str, price: float, now: int) -> Test | None:
        lvl = self._active_level
        if not lvl:
            return None
        atr = self._current_atr(symbol)
        if atr <= 0:
            return None

        if abs(price - lvl.price) > self.d_test_atr * atr:
            return None

        cutoff = now - 2000
        buy_vol = 0.0
        sell_vol = 0.0
        for ts, p, signed in reversed(self._trades.get(symbol, deque())):
            if ts < cutoff:
                break
            if signed > 0:
                buy_vol += signed
            else:
                sell_vol += abs(signed)

        median = self._median_volume(symbol)
        if median <= 0:
            return None

        if lvl.side == "support" and sell_vol >= self.k_test * median:
            return Test(side="SELL", volume=sell_vol, price=price, ts=now)
        if lvl.side == "resistance" and buy_vol >= self.k_test * median:
            return Test(side="BUY", volume=buy_vol, price=price, ts=now)
        return None

    def _check_outcome(self, symbol: str, price: float, now: int) -> None:
        lvl = self._active_level
        if not lvl or not self._active_test:
            return

        if lvl.side == "support" and price <= lvl.price - self.b_break:
            self._outcome_observed = "BREAKS"
            self._break_price = price
            self._transition(State.BREAKOUT_PENDING, f"breaks:{price:.2f}")
            return
        if lvl.side == "resistance" and price >= lvl.price + self.b_break:
            self._outcome_observed = "BREAKS"
            self._break_price = price
            self._transition(State.BREAKOUT_PENDING, f"breaks:{price:.2f}")
            return

        if now - self._state_ts >= self.w_outcome_ms:
            self._outcome_observed = "HOLDS"
            self._transition(State.REVERSAL_PENDING, f"holds:{lvl.price:.2f}")

    def _detect_flip(self, symbol: str, price: float, now: int) -> bool:
        lvl = self._active_level
        if not lvl or not self._active_test:
            return False
        atr = self._current_atr(symbol)
        if atr <= 0:
            return False

        if abs(price - lvl.price) > self.d_at_level_atr * atr:
            return False

        cutoff = now - 2000
        buy_vol = 0.0
        sell_vol = 0.0
        for ts, p, signed in reversed(self._trades.get(symbol, deque())):
            if ts < cutoff:
                break
            if signed > 0:
                buy_vol += signed
            else:
                sell_vol += abs(signed)

        if self._active_test.side == "SELL":
            if buy_vol >= self.k_flip * self._active_test.volume:
                self._flip_volume = buy_vol
                return True
        else:
            if sell_vol >= self.k_flip * self._active_test.volume:
                self._flip_volume = sell_vol
                return True
        return False

    def _detect_counter_attack(self, symbol: str, price: float, now: int) -> bool:
        lvl = self._active_level
        if not lvl or not self._active_test:
            return False

        cutoff = now - 2000
        buy_vol = 0.0
        sell_vol = 0.0
        for ts, p, signed in reversed(self._trades.get(symbol, deque())):
            if ts < cutoff:
                break
            if signed > 0:
                buy_vol += signed
            else:
                sell_vol += abs(signed)

        median = self._median_volume(symbol)
        if median <= 0:
            return False

        if self._active_test.side == "BUY":
            if sell_vol >= self.k_counter * median:
                self._counter_volume = sell_vol
                if sell_vol > 5 * median:
                    self._transition(State.IDLE, "counter_too_big_reversal")
                    return False
                return True
        else:
            if buy_vol >= self.k_counter * median:
                self._counter_volume = buy_vol
                if buy_vol > 5 * median:
                    self._transition(State.IDLE, "counter_too_big_reversal")
                    return False
                return True
        return False

    def _check_absorption(self, symbol: str, price: float, now: int) -> bool:
        lvl = self._active_level
        if not lvl or not self._active_test:
            return False

        if self._active_test.side == "BUY":
            book = self._book_bids.get(symbol, {})
        else:
            book = self._book_asks.get(symbol, {})

        if not book:
            return False

        depths = sorted(book.values())
        median_depth = depths[len(depths) // 2] if depths else 0
        if median_depth <= 0:
            return False

        window = 0.5 * self._current_atr(symbol)
        relevant = {p: q for p, q in book.items() if lvl.price - window <= p <= lvl.price + window}

        total = sum(relevant.values())
        if total < self.k_absorb * median_depth:
            return False

        if self._absorption_baseline == 0:
            self._absorption_baseline = total
        elif total < self._absorption_baseline * (1 - self.d_max_drop):
            return False

        self._absorption_depth = total

        if lvl.side == "resistance":
            if price < lvl.price - self.b_hold:
                return False
        else:
            if price > lvl.price + self.b_hold:
                return False

        return True

    def _counter_succeeded(self, symbol: str, price: float) -> bool:
        lvl = self._active_level
        if not lvl or not self._active_test:
            return True

        if self._active_test.side == "BUY":
            return price < lvl.price - self.b_hold
        else:
            return price > lvl.price + self.b_hold

    # ═══════════════════════════════════════════════════════
    # Path / Fuel / Tape spike (§3)
    # ═══════════════════════════════════════════════════════

    def _evaluate_armed(self, symbol: str, price: float, now: int, branch: str) -> None:
        if branch == "reversal":
            direction = "BUY" if self._active_test and self._active_test.side == "SELL" else "SELL"
            pattern = "SPRING" if direction == "BUY" else "FAILED_RALLY"
        else:
            direction = self._active_test.side if self._active_test else "BUY"
            pattern = "FAILED_DEFENSE_UP" if direction == "BUY" else "FAILED_DEFENSE_DOWN"

        next_wall = self._next_major_level(symbol, direction)
        if next_wall is None:
            self._transition(State.IDLE, "no_next_level")
            return
        dist_to_wall = abs(next_wall - price)
        if dist_to_wall < self.d_min_profit + self.cost_buffer:
            self._transition(State.IDLE, f"next_wall_too_close:{dist_to_wall:.2f}")
            return

        swing = self._recent_swing(symbol, direction)
        if swing is None:
            self._transition(State.IDLE, "no_swing")
            return
        dist_to_swing = abs(swing - price)
        if dist_to_swing > self.d_stop:
            self._transition(State.IDLE, f"fuel_too_far:{dist_to_swing:.2f}")
            return

        self._armed_direction = direction
        self._armed_pattern = pattern
        self._armed_ts = now
        self._trade.next_wall = next_wall
        self._trade.stop_target = swing
        self._transition(State.ARMED, f"armed:{pattern}:{direction}")

    def _next_major_level(self, symbol: str, direction: str) -> float | None:
        book = self._book_asks.get(symbol, {}) if direction == "BUY" else self._book_bids.get(symbol, {})
        if not book:
            return None
        depths = sorted(book.values())
        if not depths:
            return None
        median = depths[len(depths) // 2]
        threshold = 2.0 * median

        if direction == "BUY":
            candidates = sorted(p for p, q in book.items() if q >= threshold)
        else:
            candidates = sorted((p for p, q in book.items() if q >= threshold), reverse=True)

        ref = self._active_level.price if self._active_level else 0
        for p in candidates:
            if abs(p - ref) >= self.d_major:
                return p
        return None

    def _recent_swing(self, symbol: str, direction: str) -> float | None:
        ks = self._klines.get(symbol)
        if not ks or len(ks) < 10:
            return None
        recent = list(ks)[-10:]
        if direction == "BUY":
            return max(c["h"] for c in recent)
        else:
            return min(c["l"] for c in recent)

    def _tape_spike_confirmed(self, symbol: str, price: float, now: int) -> bool:
        cutoff = now - self.w_trig_ms
        buy_vol = 0.0
        sell_vol = 0.0
        count = 0
        for ts, p, signed in reversed(self._trades.get(symbol, deque())):
            if ts < cutoff:
                break
            count += 1
            if signed > 0:
                buy_vol += signed
            else:
                sell_vol += abs(signed)

        total = buy_vol + sell_vol
        if total <= 0:
            return False

        secs = max(1, self.w_trig_ms / 1000)
        prints_per_sec = count / secs
        median_pps = self._median_prints_per_sec(symbol)
        if median_pps <= 0 or prints_per_sec < self.tape_spike_mult * median_pps:
            return False

        delta_pct = buy_vol / total
        if self._armed_direction == "BUY" and delta_pct < self.delta_one_sided:
            return False
        if self._armed_direction == "SELL" and (1 - delta_pct) < self.delta_one_sided:
            return False

        return True

    # ═══════════════════════════════════════════════════════
    # Entry & Exit
    # ═══════════════════════════════════════════════════════

    def _enter_trade(self, symbol: str, price: float, now: int) -> None:
        lvl = self._active_level
        if not lvl:
            return

        direction = self._armed_direction
        pattern = self._armed_pattern
        atr = self._current_atr(symbol)

        self._trade = TradeContext(
            pattern=pattern,
            direction=direction,
            level=lvl,
            entry_price=price,
            entry_ts=now,
            stop_target=self._trade.stop_target,
            next_wall=self._trade.next_wall,
            mfe=price,
            mae=price,
            # Journal Metrics
            test_volume=self._active_test.volume if self._active_test else 0.0,
            flip_volume=self._flip_volume,
            absorption_depth=self._absorption_depth,
            outcome=self._outcome_observed,
            level_side=lvl.side,
        )

        self._entry_pending = True
        self._pending_entry_ts = now
        self._last_entry_pattern = pattern

        log.info(
            f"FOUR_PATTERN ENTER {direction} ({pattern}) @ {price:.2f} "
            f"| Base Zone: {lvl.price:.2f} | Target Zone: {self._trade.next_wall:.2f}"
        )

        sig = Signal(
            strategy=self.name, symbol=symbol, action=direction,
            confidence=0.85, reason=pattern, price=price,
            metadata={
                "scenario": pattern,
                "size_multiplier": self.weights.get(pattern, 1.0),
                "force_taker": True,
                "entry_plan": {
                    "setup": pattern, "side": direction, "entry_price": price,
                    "entry_ts": now, "entry_atr": atr,
                    # PURE ZONE TO ZONE: NO SL, NO TP ON BROKER
                    "stop_distance": 0.0, "stop": 0.0, "target": 0.0,
                    "base_zone": lvl.price,
                    "upper_zone": self._trade.next_wall,
                    "level_side": lvl.side,
                    "outcome": self._outcome_observed,
                    "spot_test_volume": self._trade.test_volume,
                    "flip_volume": self._trade.flip_volume,
                    "absorption_depth_observed": self._trade.absorption_depth,
                },
            },
        )
        self._emit_debug(symbol, price, sig_obj=sig)
        asyncio.create_task(bus.publish(config.protocol.topics["signal_proposed"], sig.to_dict()))

    async def _evaluate_exit(self, symbol: str, price: float, now: int) -> Signal | None:
        t = self._trade
        if not t.direction:
            return None

        is_long = t.direction == "BUY"
        age_ms = now - t.entry_ts

        if is_long:
            t.mfe = max(t.mfe, price)
            t.mae = min(t.mae, price)
        else:
            t.mfe = min(t.mfe, price)
            t.mae = max(t.mae, price)

        # 1. BASE ZONE BREAK (The structural invalidation)
        if t.level:
            if is_long and price <= t.level.price - self.b_break:
                return self._close(symbol, f"BASE_BREAK {t.level.price:.2f}", price)
            if not is_long and price >= t.level.price + self.b_break:
                return self._close(symbol, f"BASE_BREAK {t.level.price:.2f}", price)

        # 2. UPPER ZONE HOT (The structural target)
        if t.next_wall:
            if is_long and price >= t.next_wall - self.b_wall:
                return self._close(symbol, f"UPPER_ZONE {t.next_wall:.2f}", price)
            if not is_long and price <= t.next_wall + self.b_wall:
                return self._close(symbol, f"UPPER_ZONE {t.next_wall:.2f}", price)

        # 3. TIME STOP
        if age_ms > self.t_max_ms:
            return self._close(symbol, f"TIME_STOP {age_ms/1000:.0f}s", price)

        return None

    def _close(self, symbol: str, reason: str, exit_price: float) -> Signal:
        log.info(f"FOUR_PATTERN EXIT {self._trade.direction} {symbol} {reason} @ {exit_price:.2f}")
        self._state = State.CLOSED
        self._state_ts = _now_ms()
        self._emit_debug(symbol, exit_price, reason=reason)
        
        t = self._trade
        sig = Signal(
            strategy=self.name, symbol=symbol, action="CLOSE",
            confidence=0.9, reason=reason, price=exit_price,
            metadata={
                "exit": reason, 
                "pattern": t.pattern,
                "spot_exit_price": exit_price,
                "level_price": t.level.price if t.level else 0.0,
                "level_side": t.level_side,
                "outcome": t.outcome,
                "spot_test_volume": t.test_volume,
                "flip_volume": t.flip_volume,
                "absorption_depth_observed": t.absorption_depth,
            },
        )
        self._trade = TradeContext()
        return sig

    # ═══════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════

    def _update_atr(self, symbol: str) -> None:
        ks = self._klines.get(symbol)
        if not ks or len(ks) < self.atr_period + 1:
            return
        arr = list(ks)
        trs = [
            max(
                arr[i]["h"] - arr[i]["l"],
                abs(arr[i]["h"] - arr[i - 1]["c"]),
                abs(arr[i]["l"] - arr[i - 1]["c"]),
            )
            for i in range(1, len(arr))
        ]
        if len(trs) >= self.atr_period:
            atr = sum(trs[: self.atr_period]) / self.atr_period
            for tr in trs[self.atr_period:]:
                atr = ((atr * (self.atr_period - 1)) + tr) / self.atr_period
            self._atr_history.setdefault(symbol, deque(maxlen=200)).append(atr)

    def _current_atr(self, symbol: str) -> float:
        h = self._atr_history.get(symbol)
        return h[-1] if h else 0.0

    def _update_per_second_volume(self, symbol: str, ts_ms: int, signed_qty: float, side: str) -> None:
        buf = self._per_second_vol.setdefault(symbol, deque(maxlen=300))
        sec = ts_ms // 1000
        if not buf or buf[-1][0] != sec:
            buf.append((sec, 0.0, 0.0, 0))
        s, buy, sell, count = buf[-1]
        if side == "BUY":
            buy += signed_qty
        else:
            sell += abs(signed_qty)
        buf[-1] = (s, buy, sell, count + 1)

    def _median_volume(self, symbol: str) -> float:
        buf = self._per_second_vol.get(symbol)
        if not buf or len(buf) < 2:
            return 0.0
        vals = sorted(b + s for _, b, s, _ in buf)
        return vals[len(vals) // 2]

    def _median_prints_per_sec(self, symbol: str) -> float:
        buf = self._per_second_vol.get(symbol)
        if not buf or len(buf) < 2:
            return 0.0
        vals = sorted(cnt for _, _, _, cnt in buf)
        return vals[len(vals) // 2]

    def _session_ok(self, now_ms: int) -> bool:
        hour = time.gmtime(now_ms / 1000).tm_hour
        return self.session_start_hour_utc <= hour < self.session_end_hour_utc

    def _spread_ok(self, symbol: str) -> bool:
        bids = self._book_bids.get(symbol, {})
        asks = self._book_asks.get(symbol, {})
        if not bids or not asks:
            return True
        best_bid = max(bids.keys())
        best_ask = min(asks.keys())
        return (best_ask - best_bid) <= self.s_max_spread

    # ═══════════════════════════════════════════════════════
    # Telemetry
    # ═══════════════════════════════════════════════════════

    def _emit_debug(
        self,
        symbol: str,
        mid: float,
        sig_obj: Signal | None = None,
        reason: str = "",
    ) -> None:
        decision = "WAIT"
        if sig_obj is not None:
            decision = "ENTER" if sig_obj.action != "CLOSE" else "EXIT"

        payload = {
            "strategy": self.name,
            "ts": _now_ms(),
            "symbol": symbol,
            "mid": mid,
            "state": self._state.value,
            "pattern": self._active_test.side if self._active_test else "",
            "decision": decision,
            "reason": reason or (sig_obj.reason if sig_obj is not None else ""),
        }
        asyncio.create_task(bus.publish(self._topic_debug, payload))

    def dump_zones(self, symbol: str = "") -> dict[str, Any]:
        return {
            "strategy": self.name,
            "symbol": symbol or getattr(self, "_active_symbol", ""),
            "state": self._state.value,
            "active_level": {
                "price": self._active_level.price if self._active_level else 0.0,
                "side": self._active_level.side if self._active_level else "",
            } if self._active_level else None,
        }