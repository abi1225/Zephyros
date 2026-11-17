from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule

log = logging.getLogger(__name__)


class TradeJournal(BaseModule):
    """Structured per-trade journal (JSONL)."""

    name = "execution.trade_journal"

    def __init__(self) -> None:
        cfg = config.trade_journal
        self._enabled = bool(cfg.enabled)
        self._path = Path(cfg.path)
        self._flush_interval_sec = float(cfg.flush_interval_sec)
        self._max_pending = int(cfg.max_pending)

        self._signal_cache: dict[str, dict] = {}
        self._pending: list[dict] = []
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._enabled:
            log.info("TradeJournal disabled in config")
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)

        bus.subscribe(config.protocol.topics["signal_approved"], self._on_signal)
        bus.subscribe(config.protocol.topics["trade_closed"], self._on_close)

        self._flush_task = asyncio.create_task(self._flush_loop())

        log.info(f"TradeJournal active — writing to {self._path}")

    async def stop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        await self._flush()
        log.info("TradeJournal stopped")

    # ─── Signal Cache ───────────────────────────────────────

    async def _on_signal(self, _topic: str, payload: dict) -> None:
        signal_id = payload.get("id")
        if not signal_id:
            return

        if len(self._signal_cache) > 1000:
            keys = list(self._signal_cache.keys())[:200]
            for k in keys:
                self._signal_cache.pop(k, None)

        self._signal_cache[signal_id] = dict(payload)

    # ─── Close Handler ──────────────────────────────────────

    async def _on_close(self, _topic: str, payload: dict) -> None:
        signal_id = str(payload.get("signal_id", ""))
        entry_signal = self._signal_cache.pop(signal_id, {})

        entry_metadata = entry_signal.get("metadata", {}) or {}
        entry_plan = entry_metadata.get("entry_plan", {}) or {}
        exit_metadata = payload.get("signal_metadata") or {}

        # ─── §12: Pattern Specific Metrics ───
        pattern_type = entry_metadata.get("scenario")
        level_price = entry_plan.get("base_zone", 0.0)
        level_side = entry_plan.get("level_side", "")
        outcome = entry_plan.get("outcome", "")
        spot_test_volume = entry_plan.get("spot_test_volume", 0.0)
        flip_volume = entry_plan.get("flip_volume", 0.0)
        absorption_depth_observed = entry_plan.get("absorption_depth_observed", 0.0)

        # Slippage: entry fill vs signal price (Spot Entry)
        signal_price = entry_signal.get("price")
        entry_fill = payload.get("entry_price")
        slippage_pts = None
        slippage_pct = None
        if signal_price and entry_fill:
            try:
                signal_price_f = float(signal_price)
                entry_fill_f = float(entry_fill)
                if signal_price_f > 0:
                    slippage_pts = entry_fill_f - signal_price_f
                    slippage_pct = (slippage_pts / signal_price_f) * 100.0
            except (TypeError, ValueError):
                pass

        # Slippage: MT5 Close vs Spot Exit
        mt5_close_price = payload.get("exit_price")
        spot_exit_price = exit_metadata.get("spot_exit_price")
        mt5_slippage_vs_spot_exit = None
        if mt5_close_price is not None and spot_exit_price is not None:
            try:
                direction = 1.0 if payload.get("side") == "BUY" else -1.0
                mt5_slippage_vs_spot_exit = (float(mt5_close_price) - float(spot_exit_price)) * direction
            except (TypeError, ValueError):
                pass

        # R-multiple
        r_multiple = None
        try:
            pnl = float(payload.get("pnl") or 0.0)
            entry_price = float(entry_fill or 0.0)
            volume = float(payload.get("volume") or 0.0)
            if entry_price > 0 and volume > 0:
                stop_dist = float(
                    entry_plan.get("stop_distance")
                    or exit_metadata.get("stop_at_exit")
                    or 0.0
                )
                if stop_dist <= 0:
                    stop_dist = entry_price * 0.003
                risk_total = stop_dist * volume
                if risk_total > 0:
                    r_multiple = round(pnl / risk_total, 3)
        except (TypeError, ValueError):
            pass

        # MFE/MAE + capture ratio
        mfe = exit_metadata.get("mfe")
        mae = exit_metadata.get("mae")
        stop_at_exit = exit_metadata.get("stop_at_exit")
        mfe_capture = None
        pnl_raw = payload.get("pnl")
        if mfe is not None and pnl_raw is not None and entry_fill:
            try:
                mfe_profit = abs(float(mfe) - float(entry_fill))
                realized = abs(float(pnl_raw))
                if mfe_profit > 0:
                    mfe_capture = round(realized / mfe_profit, 3)
            except (TypeError, ValueError):
                pass

        journal_entry = {
            "ts": int(payload.get("ts") or time.time() * 1000),
            "entry_ts": entry_plan.get("entry_ts"),
            "duration_sec": (
                (int(payload.get("ts") or time.time() * 1000) - int(entry_plan.get("entry_ts") or 0)) / 1000.0
                if entry_plan.get("entry_ts") else None
            ),
            "ticket": payload.get("ticket"),
            "signal_id": signal_id,
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "volume": payload.get("volume"),
            "strategy": entry_signal.get("strategy"),
            
            # ─── §12 Pattern Metrics ───
            "pattern_type": pattern_type,
            "level_price": level_price,
            "level_side": level_side,
            "outcome": outcome,
            "spot_test_volume": spot_test_volume,
            "flip_volume": flip_volume,
            "absorption_depth_observed": absorption_depth_observed,
            "mt5_slippage_vs_spot_entry": round(slippage_pts, 4) if slippage_pts is not None else None,
            "mt5_slippage_vs_spot_exit": round(mt5_slippage_vs_spot_exit, 4) if mt5_slippage_vs_spot_exit is not None else None,
            
            # ─── Standard Metrics ───
            "size_multiplier": entry_metadata.get("size_multiplier"),
            "confidence": entry_signal.get("confidence"),
            "reason": entry_signal.get("reason"),
            "target": entry_plan.get("upper_zone"),
            "signal_price": signal_price,
            "entry_price": entry_fill,
            "exit_price": mt5_close_price,
            "spot_exit_price": spot_exit_price,
            "slippage_pct": round(slippage_pct, 4) if slippage_pct is not None else None,
            "pnl": payload.get("pnl"),
            "pnl_pct": payload.get("pnl_pct"),
            "r_multiple": r_multiple,
            "mfe": mfe,
            "mae": mae,
            "mfe_capture": mfe_capture,
            "stop_at_exit": stop_at_exit,
            "exit_reason": payload.get("exit_reason"),
            "exit_order_id": payload.get("exit_order_id"),
        }

        async with self._lock:
            self._pending.append(journal_entry)
            should_flush = len(self._pending) >= self._max_pending

        if should_flush:
            await self._flush()

    # ─── Flush ──────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._flush_interval_sec)
                await self._flush()
        except asyncio.CancelledError:
            return

    async def _flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []

        try:
            with self._path.open("a", encoding="utf-8") as f:
                for entry in batch:
                    f.write(json.dumps(entry, default=str) + "\n")
            log.debug(f"Flushed {len(batch)} journal entries")
        except Exception as exc:
            log.error(f"Journal flush failed: {exc}")
            async with self._lock:
                self._pending = batch + self._pending