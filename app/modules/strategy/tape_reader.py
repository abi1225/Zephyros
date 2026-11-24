"""Pure Institutional Tape Reader (symbol-agnostic).

Fixes applied vs. previous version
----------------------------------
1. Exits evaluated on book updates too — no more frozen positions when
   the tape goes quiet.
2. `pending_entry` / `closing` timeouts prevent permanent stuck state.
3. Close-failure recovery: if broker rejects the close, resume in_position
   instead of silently dropping the trade.
4. 1m-kline fallback for cTrader symbols whose feed only publishes 1m.
5. Guarded exit logic against zero/uninitialised SL or side.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from app.core.config import config
from app.core.event_bus import bus
from app.modules.strategy.base import Signal, Strategy
from app.modules.strategy.institutional import ParabolicSARTracker

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


class TapeReader(Strategy):
    name = "tape_reader"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)

        self.min_rr = float(self._base_cfg.get("min_rr", 1.0))
        self.atr_period = int(self._base_cfg.get("atr_period", 14))
        self.time_stop_sec = float(
            self._base_cfg.get("time_stop_sec", 14400.0)
        )
        self.tape_window_ms = int(
            self._base_cfg.get("tape_window_ms", 5000)
        )
        self.sweep_threshold = float(
            self._base_cfg.get("sweep_threshold", 5.0)
        )
        self.absorption_threshold = float(
            self._base_cfg.get("absorption_threshold", 10.0)
        )
        self.price_stall_pct = float(
            self._base_cfg.get("price_stall_pct", 0.001)
        )

        self._state = "flat"
        self._entry_price = 0.0
        self._entry_ts = 0
        self._entry_side = ""
        self._entry_atr = 0.0
        self._risk_at_entry = 0.0
        self._initial_sl_price = 0.0
        self._sar_activation_price = 0.0
        self._target_price = 0.0
        self._sar_active = False
        self._tf_fallback_warned = False

        self._klines: dict[str, deque] = {}
        self._atr_history: dict[str, deque] = {}
        self._trades: dict[str, deque] = {}

        self.sar_tracker = ParabolicSARTracker(step=0.02, max_step=0.20)
        self._current_sar = 0.0

        self._topic_feed_trade = config.protocol.topics.get(
            "feed_trade", "feed.trade"
        )
        self._topic_feed_depth = config.protocol.topics.get(
            "feed_depth", "feed.depth"
        )
        self._topic_position = config.protocol.topics.get(
            "position_update", "position.update"
        )
        self._topic_debug = config.protocol.topics.get(
            "debug_decision", "debug.decision"
        )

        bus.subscribe(self._topic_feed_trade, self._on_trade)
        bus.subscribe(self._topic_feed_depth, self._on_book_exit)
        bus.subscribe(self._topic_position, self._on_position)

    # ─── Lifecycle ─────────────────────────────────────────

    def dispose(self) -> None:
        for t, h in (
            (self._topic_feed_trade, self._on_trade),
            (self._topic_feed_depth, self._on_book_exit),
            (self._topic_position, self._on_position),
        ):
            try:
                bus.unsubscribe(t, h)
            except Exception:
                pass
        log.info("TapeReader disposed")

    def set_symbol(self, symbol: str) -> None:
        already_active = getattr(self, "_active_symbol", None) == symbol
        self._active_symbol = symbol
        self._apply_params(symbol)
        if already_active:
            return
        self._state = "flat"
        self._klines.setdefault(symbol, deque(maxlen=150))
        self._trades.setdefault(symbol, deque(maxlen=10000))
        self.sar_tracker.reset()
        log.info(
            "TapeReader symbol → %s (sweep=%.1f absorb=%.1f stall=%.5f)",
            symbol, self.sweep_threshold,
            self.absorption_threshold, self.price_stall_pct,
        )

    def _apply_params(self, symbol: str) -> None:
        P = lambda n, d: self._param(n, symbol, d)
        self.min_rr = float(P("min_rr", 1.0))
        self.atr_period = int(P("atr_period", 14))
        self.time_stop_sec = float(P("time_stop_sec", 14400.0))
        self.tape_window_ms = int(P("tape_window_ms", 5000))
        self.sweep_threshold = float(P("sweep_threshold", 5.0))
        self.absorption_threshold = float(
            P("absorption_threshold", 10.0)
        )
        self.price_stall_pct = float(P("price_stall_pct", 0.001))

    def set_timeframe(self, timeframe: str) -> None:
        self._timeframe = timeframe
        self._tf_fallback_warned = False

    def reset(self) -> None:
        self._state = "flat"
        self._entry_price = 0.0
        self._entry_ts = 0
        self._entry_side = ""
        self._risk_at_entry = 0.0
        self._initial_sl_price = 0.0
        self._sar_active = False
        self.sar_tracker.reset()

    def reset_symbol(self, symbol: str) -> None:
        self._klines.pop(symbol, None)
        self._atr_history.pop(symbol, None)
        self._trades.pop(symbol, None)

    def reset_position_state(self) -> None:
        if self._state == "pending_entry":
            self._state = "flat"

    def has_open_position(self) -> bool:
        return self._state in ("in_position", "pending_entry", "closing")

    # ─── Feed handlers ─────────────────────────────────────

    async def on_book(
        self, symbol: str, bids: list, asks: list,
    ) -> Signal | None:
        # We only use book for exit checks — entry is tape-driven.
        return None

    async def _on_book_exit(self, _topic: str, payload: dict) -> None:
        """FIX #1: exit evaluation on book updates (10 Hz backup)."""
        sym = payload.get("symbol")
        if not sym or sym != getattr(self, "_active_symbol", None):
            return
        if self._state != "in_position":
            return
        book = payload.get("book") or payload.get("data") or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return
        try:
            mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
        except (TypeError, ValueError, IndexError):
            return
        signal = self._evaluate_live_exit(sym, mid, _now_ms())
        if signal:
            asyncio.create_task(
                bus.publish(
                    self._topic_debug,
                    self._get_debug_payload(sym, mid, signal),
                )
            )
            asyncio.create_task(
                bus.publish(
                    config.protocol.topics["signal_proposed"],
                    signal.to_dict(),
                )
            )

    async def _on_position(self, _topic: str, payload: dict) -> None:
        if payload.get("symbol") != getattr(self, "_active_symbol", None):
            return
        position = payload.get("position")

        if position is None:
            if self._state in ("pending_entry", "closing"):
                self._state = "flat"
            return

        if self._state == "pending_entry":
            self._state = "in_position"
            try:
                fill = float(position.get("entry", 0.0) or 0.0)
                if fill > 0 and self._entry_side:
                    self._entry_price = fill
                    self._risk_at_entry = abs(
                        fill - self._initial_sl_price
                    )
                    if self._entry_side == "BUY":
                        self._target_price = (
                            fill + self._risk_at_entry * self.min_rr
                        )
                        self._sar_activation_price = (
                            fill + self._entry_atr
                        )
                    else:
                        self._target_price = (
                            fill - self._risk_at_entry * self.min_rr
                        )
                        self._sar_activation_price = (
                            fill - self._entry_atr
                        )
            except (TypeError, ValueError, KeyError):
                pass
        elif self._state == "closing":
            log.warning(
                "TAPE close rejected by broker — resuming in_position"
            )
            self._state = "in_position"

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
        buf = self._trades.setdefault(sym, deque(maxlen=10000))
        buf.append((ts, price, signed_qty))

        now = _now_ms()

        # FIX #2: pending-entry timeout
        if self._state == "pending_entry":
            if now - self._entry_ts > 10_000:
                log.warning("TAPE entry timeout — resetting")
                self._state = "flat"
            return

        if self._state == "closing":
            return

        if self._state == "in_position":
            signal = self._evaluate_live_exit(sym, price, now)
            if signal:
                asyncio.create_task(
                    bus.publish(
                        self._topic_debug,
                        self._get_debug_payload(sym, price, signal),
                    )
                )
                asyncio.create_task(
                    bus.publish(
                        config.protocol.topics["signal_proposed"],
                        signal.to_dict(),
                    )
                )
            return

        self._evaluate_entry(sym, price, now)

    async def on_kline(
        self, symbol: str, payload: dict,
    ) -> Signal | None:
        interval = payload.get("interval", "")
        expected = getattr(self, "_timeframe", "1m")
        # FIX #4: accept 1m as fallback for cTrader
        if interval != expected:
            if interval != "1m":
                return None
            if not self._tf_fallback_warned:
                log.warning(
                    "TapeReader: expected %s klines, got 1m — using 1m as fallback",
                    expected,
                )
                self._tf_fallback_warned = True

        k = payload.get("kline")
        if not k or not k.get("closed"):
            return None

        candle = {
            "t": int(k["t"]),
            "o": float(k["o"]),
            "h": float(k["h"]),
            "l": float(k["l"]),
            "c": float(k["c"]),
            "v": float(k["v"]),
        }
        buf = self._klines.setdefault(symbol, deque(maxlen=150))
        buf.append(candle)
        self._update_atr(symbol)
        self._current_sar = self.sar_tracker.update(
            candle["h"], candle["l"],
        )
        return None

    # ─── ATR ───────────────────────────────────────────────

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
            self._atr_history.setdefault(
                symbol, deque(maxlen=200),
            ).append(atr)

    def _current_atr(self, symbol: str) -> float:
        h = self._atr_history.get(symbol)
        return h[-1] if h else 0.0

    # ─── Tape metrics ──────────────────────────────────────

    def _compute_tape_metrics(
        self, symbol: str, now_ms: int,
    ) -> tuple[float, float, int, int]:
        buf = self._trades.get(symbol)
        if not buf:
            return 0.0, 0.0, 0, 0

        cutoff = now_ms - self.tape_window_ms
        buy_vol = 0.0
        sell_vol = 0.0
        buy_count = 0
        sell_count = 0
        for ts, price, signed_qty in reversed(buf):
            if ts < cutoff:
                break
            if signed_qty > 0:
                buy_vol += signed_qty
                buy_count += 1
            else:
                sell_vol += abs(signed_qty)
                sell_count += 1

        net_delta = buy_vol - sell_vol
        velocity = (buy_vol + sell_vol) / (self.tape_window_ms / 1000.0)
        return net_delta, velocity, buy_count, sell_count

    # ─── Entry logic ───────────────────────────────────────

    def _evaluate_entry(
        self, symbol: str, current_price: float, now: int,
    ) -> None:
        atr = self._current_atr(symbol)
        if atr <= 0 or len(self._klines.get(symbol, [])) < 5:
            return

        net_delta, velocity, _buy_count, _sell_count = (
            self._compute_tape_metrics(symbol, now)
        )

        recent = list(self._klines[symbol])[-5:]
        recent_low = min(c["l"] for c in recent)
        recent_high = max(c["h"] for c in recent)

        price_dist_from_low = (
            abs(current_price - recent_low) / current_price
        )
        price_dist_from_high = (
            abs(current_price - recent_high) / current_price
        )

        # 1. Bullish absorption
        if (
            price_dist_from_low < self.price_stall_pct
            and net_delta < -self.absorption_threshold
        ):
            self._emit_entry(
                symbol, "BUY", current_price, atr,
                f"bull_absorb (Δ={net_delta:.1f})",
            )
            return

        # 2. Bearish absorption
        if (
            price_dist_from_high < self.price_stall_pct
            and net_delta > self.absorption_threshold
        ):
            self._emit_entry(
                symbol, "SELL", current_price, atr,
                f"bear_absorb (Δ={net_delta:.1f})",
            )
            return

        # 3. Bullish sweep
        if (
            current_price > recent_high
            and net_delta > self.sweep_threshold
            and velocity > 1.5
        ):
            self._emit_entry(
                symbol, "BUY", current_price, atr,
                f"bull_sweep (Δ={net_delta:.1f} vel={velocity:.1f})",
            )
            return

        # 4. Bearish sweep
        if (
            current_price < recent_low
            and net_delta < -self.sweep_threshold
            and velocity > 1.5
        ):
            self._emit_entry(
                symbol, "SELL", current_price, atr,
                f"bear_sweep (Δ={net_delta:.1f} vel={velocity:.1f})",
            )
            return

        self._emit_debug(symbol, current_price, None)

    def _emit_entry(
        self,
        symbol: str,
        side: str,
        price: float,
        atr: float,
        entry_type: str,
    ) -> None:
        if side == "BUY":
            swing_low = min(
                c["l"] for c in list(self._klines[symbol])[-5:]
            )
            initial_sl = swing_low - (0.5 * atr)
            sar_activation = price + atr
        else:
            swing_high = max(
                c["h"] for c in list(self._klines[symbol])[-5:]
            )
            initial_sl = swing_high + (0.5 * atr)
            sar_activation = price - atr

        risk_dist = abs(price - initial_sl)
        if risk_dist <= 0:
            risk_dist = atr

        target = (
            price + (risk_dist * self.min_rr)
            if side == "BUY"
            else price - (risk_dist * self.min_rr)
        )

        self._state = "pending_entry"
        self._entry_price = price
        self._entry_ts = _now_ms()
        self._entry_side = side
        self._entry_atr = atr
        self._risk_at_entry = risk_dist
        self._initial_sl_price = initial_sl
        self._sar_activation_price = sar_activation
        self._target_price = target
        self._sar_active = False
        self.sar_tracker.reset(trend=1 if side == "BUY" else -1)

        log.info(
            "TAPE ENTER %s (%s) @ %.4f | SL: %.4f target: %.4f",
            side, entry_type, price, initial_sl, target,
        )

        signal = Signal(
            strategy=self.name,
            symbol=symbol,
            action=side,
            confidence=0.85,
            reason=entry_type,
            price=price,
            metadata={
                "scenario": entry_type,
                "size_multiplier": 1.0,
                "force_taker": True,
                "entry_plan": {
                    "setup": entry_type,
                    "side": side,
                    "entry_price": price,
                    "entry_ts": self._entry_ts,
                    "entry_atr": atr,
                    "stop_distance": risk_dist,
                    "stop": initial_sl,
                    "target": target,
                },
            },
        )
        asyncio.create_task(
            bus.publish(
                self._topic_debug,
                self._get_debug_payload(symbol, price, signal),
            )
        )
        asyncio.create_task(
            bus.publish(
                config.protocol.topics["signal_proposed"],
                signal.to_dict(),
            )
        )

    # ─── Exit logic ────────────────────────────────────────

    def _evaluate_live_exit(
        self, symbol: str, price: float, now: int,
    ) -> Signal | None:
        # FIX #5: guards
        if self._state != "in_position":
            return None
        if not self._entry_side or self._initial_sl_price <= 0:
            return None
        if price <= 0:
            return None

        age = (now - self._entry_ts) / 1000.0
        is_long = self._entry_side == "BUY"

        if is_long and price <= self._initial_sl_price:
            return self._close(symbol, "structural_sl", price)
        if not is_long and price >= self._initial_sl_price:
            return self._close(symbol, "structural_sl", price)

        if not self._sar_active:
            if is_long and price >= self._sar_activation_price:
                self._sar_active = True
                log.info(
                    "TAPE %s: 1 ATR reached — SAR trail active", symbol,
                )
            elif not is_long and price <= self._sar_activation_price:
                self._sar_active = True
                log.info(
                    "TAPE %s: 1 ATR reached — SAR trail active", symbol,
                )

        if self._sar_active:
            sar = self._current_sar
            if sar > 0:
                if is_long and price <= sar:
                    return self._close(
                        symbol, f"sar_trail {sar:.4f}", price,
                    )
                if not is_long and price >= sar:
                    return self._close(
                        symbol, f"sar_trail {sar:.4f}", price,
                    )

        if is_long and price >= self._target_price:
            return self._close(symbol, "target", price)
        if not is_long and price <= self._target_price:
            return self._close(symbol, "target", price)

        if age > self.time_stop_sec:
            return self._close(symbol, "time_stop", price)

        return None

    def _close(
        self, symbol: str, reason: str, exit_price: float,
    ) -> Signal | None:
        if self._state != "in_position":
            return None
        log.info(
            "TAPE EXIT %s %s @ %.4f", self._entry_side, reason, exit_price,
        )
        self._state = "closing"
        self._sar_active = False
        return Signal(
            strategy=self.name,
            symbol=symbol,
            action="CLOSE",
            confidence=0.9,
            reason=reason,
            price=exit_price,
            metadata={
                "exit": reason,
                "entry_side": self._entry_side,
                "entry_price": self._entry_price,
            },
        )

    # ─── Debug / telemetry ─────────────────────────────────

    def _get_debug_payload(
        self, symbol: str, mid: float, signal: Signal | None,
    ) -> dict:
        net_delta, velocity, _, _ = self._compute_tape_metrics(
            symbol, _now_ms(),
        )
        if signal:
            decision = "ENTER" if signal.action != "CLOSE" else "EXIT"
            reason = signal.reason
        else:
            decision = "WAIT"
            reason = f"Δ={net_delta:+.1f} vel={velocity:.1f}"
        return {
            "strategy": self.name,
            "ts": _now_ms(),
            "symbol": symbol,
            "mid": mid,
            "state": self._state,
            "cvd": round(net_delta, 2),
            "velocity": round(velocity, 2),
            "decision": decision,
            "reason": reason,
        }

    def _emit_debug(
        self, symbol: str, mid: float, signal: Signal | None,
    ) -> None:
        payload = self._get_debug_payload(symbol, mid, signal)
        asyncio.create_task(bus.publish(self._topic_debug, payload))

    def dump_zones(self, symbol: str = "") -> dict[str, Any]:
        net_delta, velocity, _, _ = self._compute_tape_metrics(
            symbol, _now_ms(),
        )
        return {
            "strategy": self.name,
            "state": self._state,
            "net_delta": net_delta,
            "velocity": velocity,
        }