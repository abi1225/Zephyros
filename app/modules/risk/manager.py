from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.config import config
from app.core.database import get_db
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    day: str = ""
    starting_equity: float = 0.0
    daily_realized_pnl: float = 0.0
    daily_trades: int = 0
    daily_wins: int = 0
    daily_losses: int = 0
    trading_halted: bool = False
    halt_reason: str = ""
    open_positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_close_ts: dict[str, int] = field(default_factory=dict)
    last_loss_ts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "starting_equity": self.starting_equity,
            "daily_realized_pnl": self.daily_realized_pnl,
            "daily_trades": self.daily_trades,
            "daily_wins": self.daily_wins,
            "daily_losses": self.daily_losses,
            "trading_halted": self.trading_halted,
            "halt_reason": self.halt_reason,
            "open_positions": self.open_positions,
            "last_close_ts": self.last_close_ts,
            "last_loss_ts": self.last_loss_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RiskState":
        s = cls()
        s.day = d.get("day", "")
        s.starting_equity = float(d.get("starting_equity", 0.0))
        s.daily_realized_pnl = float(d.get("daily_realized_pnl", 0.0))
        s.daily_trades = int(d.get("daily_trades", 0))
        s.daily_wins = int(d.get("daily_wins", 0))
        s.daily_losses = int(d.get("daily_losses", 0))
        s.trading_halted = bool(d.get("trading_halted", False))
        s.halt_reason = str(d.get("halt_reason", ""))
        s.open_positions = dict(d.get("open_positions", {}) or {})
        s.last_close_ts = {k: int(v) for k, v in (d.get("last_close_ts") or {}).items()}
        s.last_loss_ts = {k: int(v) for k, v in (d.get("last_loss_ts") or {}).items()}
        return s


@dataclass
class RiskDecision:
    approved: bool
    reason: str = ""


class RiskManager(BaseModule):
    name = "risk.manager"

    def __init__(self) -> None:
        self._state = RiskState()
        self._db = get_db()
        self._state_path = Path(config.app.risk_state_path)
        self._running = False
        self._best: dict[str, tuple[float, float]] = {}
        self._pnl_task: asyncio.Task | None = None
        # Lot size the ribbon asked for; mirrors MT5Executor's default so
        # the notional cap can be evaluated before a signal is executed.
        self._default_volume: float = 0.01

    async def start(self) -> None:
        self._running = True
        self._load_state()
        self._roll_day_if_needed()
        if self._state.day == self._current_day():
            # Fresh boot on the same day: the in-memory counter may be
            # stale or empty, so reconcile against the trade database.
            self._restore_day_from_db()

        bus.subscribe(config.protocol.topics["signal_proposed"], self._on_proposed)
        bus.subscribe(config.protocol.topics["position_update"], self._on_position)
        bus.subscribe(config.protocol.topics["trade_closed"], self._on_closed)
        bus.subscribe(
            config.protocol.topics.get("account_update", "account_update"),
            self._on_account,
        )
        bus.subscribe(
            config.protocol.topics.get("cmd_clear_risk_state", "cmd_clear_risk_state"),
            self._on_clear_state,
        )
        bus.subscribe(
            config.protocol.topics.get("book_update", "book_update"),
            self._on_book,
        )
        bus.subscribe(
            config.protocol.topics.get("cmd_start_bot", "cmd_start_bot"),
            self._on_start_cmd,
        )

        asyncio.create_task(self._publish_loop())

        await self._log(
            f"RiskManager online — day={self._state.day} "
            f"open={len(self._state.open_positions)} "
            f"pnl={self._state.daily_realized_pnl:+.2f} "
            f"equity={self._state.starting_equity:.2f} "
            f"halted={self._state.trading_halted}"
        )

    async def _on_start_cmd(self, _topic: str, payload: dict) -> None:
        """Track the lot size chosen in the ribbon for the notional cap."""
        values = (payload or {}).get("values") or {}
        vol = values.get("volume")
        if vol is not None:
            try:
                v = float(vol)
                if v > 0:
                    self._default_volume = v
            except (TypeError, ValueError):
                pass

    async def _on_book(self, _topic: str, payload: dict) -> None:
        """Track best bid/ask for the live spread gate."""
        symbol = payload.get("symbol")
        data = payload.get("data") or {}
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not symbol or not bids or not asks:
            return
        try:
            self._best[symbol] = (float(bids[0][0]), float(asks[0][0]))
        except (TypeError, ValueError, IndexError):
            return

    async def stop(self) -> None:
        self._running = False
        self._save_state()

    async def _on_clear_state(self, _topic: str, payload: dict) -> None:
        """Explicitly clear position tracking for one or all symbols.
        Used by the pipeline test to remove stale state before running."""
        symbol = str((payload or {}).get("symbol", "")).strip()
        if symbol:
            self._state.open_positions.pop(symbol, None)
            self._state.last_close_ts.pop(symbol, None)
            self._state.last_loss_ts.pop(symbol, None)
        else:
            self._state.open_positions.clear()
            self._state.last_close_ts.clear()
            self._state.last_loss_ts.clear()
        self._save_state()
        await self._log(
            f"RiskManager: cleared state for {symbol or 'ALL symbols'}"
        )

    async def _on_account(self, _topic: str, payload: dict) -> None:
        try:
            equity = float(payload.get("equity") or 0.0)
        except (TypeError, ValueError):
            return
        if equity <= 0:
            return

        if self._state.starting_equity <= 0:
            self._state.starting_equity = equity
            self._save_state()
            await self._log(f"Captured starting equity: {equity:.2f}")
            return

        # Equity-based halt: an open drawdown must count too, not just
        # trades that have already been closed.
        if self._state.trading_halted:
            return
        drawdown_pct = (
            (self._state.starting_equity - equity)
            / self._state.starting_equity * 100.0
        )
        if drawdown_pct >= config.risk.max_daily_loss_pct:
            self._state.trading_halted = True
            self._state.halt_reason = (
                f"equity drawdown {drawdown_pct:.2f}% >= "
                f"{config.risk.max_daily_loss_pct}%"
            )
            self._save_state()
            await self._log(
                f"TRADING HALTED — {self._state.halt_reason}", level="error",
            )

    def _load_state(self) -> None:
        if not config.risk.persist_state:
            return
        if not self._state_path.exists():
            self._state = RiskState()
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._state = RiskState.from_dict(raw)
        except Exception as exc:
            log.warning("Failed to load risk state: %s", exc)
            self._state = RiskState()

    def _save_state(self) -> None:
        if not config.risk.persist_state:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._state.to_dict(), indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)
        except Exception as exc:
            log.warning("Failed to save risk state: %s", exc)

    def _signal_volume(self, metadata: dict) -> float:
        """Best estimate of the volume this signal will trade.

        Only `volume_override` is unambiguous here. The executor's default
        lot size is not visible to the risk manager, so a signal without an
        explicit override returns 0.0 and skips the notional check rather
        than guessing a size it does not know.
        """
        try:
            override = metadata.get("volume_override")
            if override is not None:
                return max(float(override), 0.0)
        except (TypeError, ValueError):
            pass
        return 0.0

    def _contract_size(self, symbol: str) -> float:
        """Units per 1.0 lot, used to convert volume to exposure."""
        sym = registry.get(symbol)
        if sym is None:
            return 1.0
        try:
            return float(getattr(sym, "contract_size", 1.0) or 1.0)
        except (TypeError, ValueError):
            return 1.0

    def _current_day(self) -> str:
        reset_hour = int(config.risk.daily_reset_hour_utc) % 24
        now_utc = datetime.now(timezone.utc)
        shifted = now_utc - timedelta(hours=reset_hour)
        return shifted.strftime("%Y-%m-%d")

    def _roll_day_if_needed(self) -> None:
        today = self._current_day()
        if self._state.day == today:
            return
        log.info("Risk day rollover: %s → %s", self._state.day, today)
        self._state.day = today
        self._state.starting_equity = 0.0
        self._state.daily_realized_pnl = 0.0
        self._state.daily_trades = 0
        self._state.daily_wins = 0
        self._state.daily_losses = 0
        self._state.trading_halted = False
        self._state.halt_reason = ""
        self._restore_day_from_db()
        self._save_state()

    def _day_start_ms(self) -> int:
        reset_hour = int(config.risk.daily_reset_hour_utc) % 24
        now_utc = datetime.now(timezone.utc)
        start = now_utc.replace(
            hour=reset_hour, minute=0, second=0, microsecond=0,
        )
        if start > now_utc:
            start -= timedelta(days=1)
        return int(start.timestamp() * 1000)

    def _restore_day_from_db(self) -> None:
        """Rebuild today's realised PnL from closed trades.

        Without this a restart mid-drawdown resets the daily loss counter
        and the cap can be sidestepped by relaunching the app.
        """
        try:
            since = self._day_start_ms()
            pnl = self._db.realized_pnl_since(since)
            counts = self._db.trade_counts_since(since)
        except Exception as exc:
            log.warning("Risk state restore from DB failed: %s", exc)
            return
        self._state.daily_realized_pnl = float(pnl)
        self._state.daily_trades = int(counts.get("total", 0))
        self._state.daily_wins = int(counts.get("wins", 0))
        self._state.daily_losses = int(counts.get("losses", 0))
        log.info(
            "Risk state restored from DB: pnl=%+.2f trades=%d (%dW/%dL)",
            self._state.daily_realized_pnl,
            self._state.daily_trades,
            self._state.daily_wins,
            self._state.daily_losses,
        )

    async def _on_proposed(self, _topic: str, payload: dict) -> None:
        log.info(f"[PIPELINE 2→3] Risk manager received: {payload.get('action')} {payload.get('symbol')}")
        self._roll_day_if_needed()

        signal_id = str(payload.get("id", ""))
        symbol = str(payload.get("symbol", ""))
        action = str(payload.get("action", ""))
        ts = int(payload.get("ts", 0))
        strategy = str(payload.get("strategy", ""))
        confidence = float(payload.get("confidence", 0.0))
        reason = str(payload.get("reason", ""))
        metadata = payload.get("metadata") or {}

        decision = self._evaluate(symbol, action, ts, metadata)
        log.info(f"[PIPELINE 3] Risk decision: approved={decision.approved} reason={decision.reason}")

        try:
            self._db.insert_signal(
                signal_id=signal_id,
                ts=ts or int(time.time() * 1000),
                strategy=strategy,
                symbol=symbol,
                action=action,
                confidence=confidence,
                reason=reason,
                approved=decision.approved,
                reject_reason=decision.reason if not decision.approved else None,
                metadata=metadata,
            )
        except Exception as exc:
            log.warning("DB insert_signal failed: %s", exc)

        if decision.approved:
            await bus.publish(
                config.protocol.topics["signal_approved"],
                payload,
            )
            await bus.publish(
                config.protocol.topics["signal_new"],
                payload,
            )
        else:
            rejected = dict(payload)
            rejected["reject_reason"] = decision.reason
            await bus.publish(
                config.protocol.topics["signal_rejected"],
                rejected,
            )
            await bus.publish(
                config.protocol.topics["signal_new"],
                rejected,
            )
            await self._log(
                f"Rejected {action} {symbol}: {decision.reason}",
                level="warn",
            )

    def _evaluate(self, symbol: str, action: str, ts: int, metadata: dict) -> RiskDecision:
        if action not in ("BUY", "SELL"):
            return RiskDecision(approved=True)

        if self._state.trading_halted:
            return RiskDecision(
                approved=False,
                reason=f"halted: {self._state.halt_reason or 'daily loss cap'}",
            )

        if config.risk.no_pyramiding and symbol in self._state.open_positions:
            return RiskDecision(
                approved=False,
                reason=f"already positioned on {symbol}",
            )

        if len(self._state.open_positions) >= config.risk.max_open_positions:
            return RiskDecision(
                approved=False,
                reason=(
                    f"max open positions "
                    f"({len(self._state.open_positions)}/"
                    f"{config.risk.max_open_positions})"
                ),
            )

        now_ms = int(time.time() * 1000)

        # ─── Spread gate ───
        max_spread = config.risk.max_spread.get(symbol)
        if max_spread is not None:
            best = self._best.get(symbol)
            if best is None:
                return RiskDecision(
                    approved=False,
                    reason=f"no live book for {symbol} (spread unknown)",
                )
            bid, ask = best
            spread = ask - bid
            if spread > float(max_spread):
                return RiskDecision(
                    approved=False,
                    reason=f"spread {spread:.4f} > max {float(max_spread):.4f}",
                )

        # ─── Notional / position-size cap ───
        best = self._best.get(symbol)
        price = ((best[0] + best[1]) / 2.0) if best else 0.0
        if price > 0 and self._state.starting_equity > 0:
            volume = self._signal_volume(metadata) or self._default_volume
            if volume > 0:
                cap = (
                    float(config.risk.max_position_pct) / 100.0
                    * self._state.starting_equity
                )
                # BTC-style contracts are already notional; forex/gold lots
                # are quoted per 100k units, so scale by contract_size.
                exposure = volume * price * self._contract_size(symbol)
                if cap > 0 and exposure > cap:
                    return RiskDecision(
                        approved=False,
                        reason=(
                            f"position size {volume} ≈ {exposure:,.0f} "
                            f"exceeds {config.risk.max_position_pct:.1f}% "
                            f"of equity ({cap:,.0f})"
                        ),
                    )

        last_close = self._state.last_close_ts.get(symbol, 0)
        if last_close:
            elapsed = (now_ms - last_close) / 1000.0
            if elapsed < config.risk.cooldown_after_exit_sec:
                return RiskDecision(
                    approved=False,
                    reason=(f"post-exit cooldown ({elapsed:.1f}s < {config.risk.cooldown_after_exit_sec}s)"),
                )

        last_loss = self._state.last_loss_ts.get(symbol, 0)
        if last_loss:
            elapsed = (now_ms - last_loss) / 1000.0
            if elapsed < config.risk.cooldown_after_loss_sec:
                return RiskDecision(
                    approved=False,
                    reason=(f"post-loss cooldown ({elapsed:.1f}s < {config.risk.cooldown_after_loss_sec}s)"),
                )

        if ts:
            age = (now_ms - ts) / 1000.0
            if age > config.risk.max_signal_age_sec:
                return RiskDecision(
                    approved=False,
                    reason=f"stale signal ({age:.1f}s)",
                )

        return RiskDecision(approved=True)

    async def _on_position(self, _topic: str, payload: dict) -> None:
        symbol = payload.get("symbol")
        position = payload.get("position")
        if not symbol:
            return

        if position is None:
            self._state.open_positions.pop(symbol, None)
        else:
            self._state.open_positions[symbol] = dict(position)
        self._save_state()

    async def _on_closed(self, _topic: str, payload: dict) -> None:
        self._roll_day_if_needed()

        symbol = str(payload.get("symbol", ""))
        pnl = float(payload.get("pnl") or 0.0)
        pnl_pct = float(payload.get("pnl_pct") or 0.0)
        exit_reason = str(payload.get("exit_reason", "unknown"))
        now_ms = int(time.time() * 1000)

        self._state.open_positions.pop(symbol, None)
        self._state.last_close_ts[symbol] = now_ms
        self._state.daily_realized_pnl += pnl
        self._state.daily_trades += 1

        if pnl > 0:
            self._state.daily_wins += 1
        elif pnl < 0:
            self._state.daily_losses += 1
            self._state.last_loss_ts[symbol] = now_ms

        try:
            entry_ticket = payload.get("ticket")
            if entry_ticket:
                self._db.close_trade(
                    entry_order_id=int(entry_ticket),
                    exit_order_id=payload.get("exit_order_id"),
                    exit_price=payload.get("exit_price"),
                    exit_ts=now_ms,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    exit_reason=exit_reason,
                )
        except Exception as exc:
            log.warning("DB close_trade failed: %s", exc)

        if self._state.starting_equity > 0:
            loss_pct = (-self._state.daily_realized_pnl / self._state.starting_equity * 100.0)
            if loss_pct >= config.risk.max_daily_loss_pct:
                self._state.trading_halted = True
                self._state.halt_reason = (
                    f"daily loss cap ({loss_pct:.2f}% >= {config.risk.max_daily_loss_pct}%)"
                )
                await self._log(
                    f"TRADING HALTED — {self._state.halt_reason}",
                    level="error",
                )

        self._save_state()

        await self._log(
            f"Trade closed {symbol} pnl={pnl:+.2f} ({pnl_pct:+.2f}%) "
            f"reason={exit_reason} daily_pnl={self._state.daily_realized_pnl:+.2f}"
        )

    async def _publish_loop(self) -> None:
        topic = config.protocol.topics["risk_state"]
        interval = max(config.risk.publish_interval_sec, 1)
        while self._running:
            await self._publish_state()
            await asyncio.sleep(interval)

    async def _publish_state(self) -> None:
        payload = self._state.to_dict()
        payload["cooldown_exit_sec"] = config.risk.cooldown_after_exit_sec
        payload["cooldown_loss_sec"] = config.risk.cooldown_after_loss_sec
        payload["max_open_positions"] = config.risk.max_open_positions
        payload["max_daily_loss_pct"] = config.risk.max_daily_loss_pct
        payload["ts"] = int(time.time() * 1000)
        await bus.publish(config.protocol.topics["risk_state"], payload)

    async def _log(self, message: str, level: str = "info") -> None:
        await bus.publish(
            config.protocol.topics["log_emit"],
            {"level": level, "module": self.name, "message": message},
        )