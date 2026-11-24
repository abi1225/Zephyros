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

    async def start(self) -> None:
        self._running = True
        self._load_state()
        self._roll_day_if_needed()

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

        asyncio.create_task(self._publish_loop())

        await self._log(
            f"RiskManager online — day={self._state.day} "
            f"open={len(self._state.open_positions)} "
            f"pnl={self._state.daily_realized_pnl:+.2f} "
            f"equity={self._state.starting_equity:.2f} "
            f"halted={self._state.trading_halted}"
        )

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
        if self._state.starting_equity > 0:
            return
        try:
            equity = float(payload.get("equity") or 0.0)
        except (TypeError, ValueError):
            return
        if equity <= 0:
            return
        self._state.starting_equity = equity
        self._save_state()
        await self._log(f"Captured starting equity: {equity:.2f}")

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
        self._save_state()

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

        if metadata.get("bypass_risk"):
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