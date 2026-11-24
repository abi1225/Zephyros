from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.core.config import config
from app.core.database import get_db
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry
from app.core.kline_router import kline_router
from app.modules.strategy.base import Signal, Strategy
from app.modules.strategy.registry import STRATEGY_REGISTRY
from app.modules.strategy.schemas import get_schema, get_strategy_db_table

log = logging.getLogger(__name__)


class StrategyManager(BaseModule):
    """Owns one Strategy instance for the whole session.

    The instance is created once (per active strategy selection) and
    REUSED across symbols. On symbol switch or START, we call
    `set_symbol()`, which refreshes per-symbol params and resets the
    state machine — but the object identity is preserved, so warmup
    buffers stay in memory.

    Symbol switches are blocked while a position is open, because the
    FeedRouter only runs one feed at a time — we cannot evaluate exit
    conditions for a symbol whose data is not flowing.
    """

    name = "strategy.manager"

    def __init__(self) -> None:
        self._running = False
        self._strategy: Strategy | None = None
        self._symbol: str | None = None
        self._timeframe: str = "1m"
        self._signal_count: int = 0
        self._started_at: int = 0
        self._auto_log_task: asyncio.Task | None = None
        self._db = get_db()

    # ─── Lifecycle ─────────────────────────────────────────

    async def start(self) -> None:
        t = config.protocol.topics
        bus.subscribe(t["cmd_start_bot"], self._on_start_cmd)
        bus.subscribe(t["cmd_stop_bot"], self._on_stop_cmd)
        bus.subscribe(t["cmd_log_zones"], self._on_log_zones)
        bus.subscribe(
            t.get("cmd_select_strategy", "cmd_select_strategy"),
            self._on_select_strategy,
        )
        bus.subscribe(
            t.get("signal_failed", "signal_failed"),
            self._on_signal_failed,
        )
        bus.subscribe(
            t.get("ui_symbol_changed", "ui_symbol_changed"),
            self._on_symbol_changed,
        )
        bus.subscribe(t["feed_depth"], self._on_book)
        bus.subscribe(t["feed_kline"], self._on_kline)

        asyncio.create_task(self._zones_publisher_loop())
        self._load_active_strategy()

        await bus.publish(t["strategy_state"], {
            "warming": False, "running": False, "ready": True,
            "strategy": self._strategy.name if self._strategy else None,
            "symbol": None, "signal_count": 0, "started_at": 0,
        })
        await self._announce_strategy()
        log.info("StrategyManager ready — START button unlocked")

    async def stop(self) -> None:
        self._running = False
        self._cancel_auto_log()
        if self._strategy is not None:
            try:
                self._strategy.dispose()
            except Exception:
                log.exception("Strategy dispose() failed on shutdown")

    # ─── Strategy load / swap ──────────────────────────────

    def _load_active_strategy(self) -> None:
        self._swap_strategy(config.strategies.active)

    def _swap_strategy(self, name: str) -> None:
        """Create a fresh strategy instance. `per_symbol` stays in cfg
        so `_param()` can resolve overrides later."""
        klass = STRATEGY_REGISTRY.get(name)
        if klass is None:
            raise KeyError(f"Unknown strategy: {name!r}")

        strat_dump = config.strategies.model_dump()
        cfg: dict = dict(strat_dump.get(name, {}) or {})
        # NOTE: do NOT pop "per_symbol" — it is consumed by Strategy._param.

        for key in ("cooldown_ms", "min_confidence"):
            if key in strat_dump and key not in cfg:
                cfg[key] = strat_dump[key]

        self._strategy = klass(cfg)
        self._symbol = None
        log.info("Loaded strategy %r (base cfg; per-symbol resolved lazily).", name)

    async def _announce_strategy(self) -> None:
        if self._strategy is None:
            return
        name = self._strategy.name
        schema = get_schema(name)
        db_table = get_strategy_db_table(name) or ""
        await bus.publish(
            config.protocol.topics.get("strategy_changed", "strategy_changed"),
            {
                "strategy": name,
                "label": schema.get("label", name),
                "db_table": db_table,
            },
        )

    async def _on_select_strategy(self, _topic: str, payload: dict) -> None:
        name = str((payload or {}).get("strategy", "")).strip()
        if not name:
            return
        if self._strategy is not None and self._strategy.name == name:
            return

        old_name = self._strategy.name if self._strategy else "—"
        self._running = False
        self._cancel_auto_log()

        if self._strategy is not None:
            try:
                self._strategy.dispose()
            except Exception:
                log.exception("Strategy dispose() failed during swap")

        try:
            self._swap_strategy(name)
        except KeyError as exc:
            log.error("Strategy swap rejected: %s", exc)
            try:
                self._swap_strategy(old_name)
            except Exception:
                self._strategy = None
            return
        except Exception:
            log.exception("Strategy swap failed for %s", name)
            return

        await bus.publish(config.protocol.topics["strategy_state"], {
            "warming": False, "running": False, "ready": True,
            "strategy": self._strategy.name if self._strategy else None,
            "symbol": None, "signal_count": 0, "started_at": 0,
        })
        await self._announce_strategy()

    # ─── Symbol change handler ─────────────────────────────

    async def _on_symbol_changed(self, _topic: str, payload: dict) -> None:
        new_symbol = str((payload or {}).get("symbol", "")).strip()
        if not new_symbol or not self._running or self._strategy is None:
            return
        if new_symbol == self._symbol:
            return

        # Block switch if a position is open on the current symbol.
        # The FeedRouter will stop the old feed, so we would lose the
        # ability to evaluate exit conditions.
        if self._strategy_has_position():
            msg = (
                f"Symbol switch blocked: open position on {self._symbol}. "
                f"Close it before switching to {new_symbol}."
            )
            log.warning(msg)
            await bus.publish(
                config.protocol.topics["log_emit"],
                {"level": "warn", "module": self.name, "message": msg},
            )
            return

        # Safe switch: refresh per-symbol params, warm klines, keep running.
        self._strategy.set_symbol(new_symbol)
        self._strategy.set_timeframe(self._timeframe)
        self._symbol = new_symbol

        try:
            klines = await asyncio.wait_for(
                kline_router.fetch(new_symbol, self._timeframe, limit=100),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            log.warning("Kline warmup timed out for %s after switch", new_symbol)
            klines = []
        except Exception:
            log.exception("Kline warmup failed for %s after switch", new_symbol)
            klines = []

        for kp in klines:
            try:
                await self._strategy.on_kline(new_symbol, kp)
            except Exception:
                pass

        await bus.publish(config.protocol.topics["strategy_state"], {
            "warming": False, "running": True, "ready": True,
            "strategy": self._strategy.name, "symbol": new_symbol,
            "signal_count": self._signal_count, "started_at": self._started_at,
        })
        log.info(
            "Strategy %s switched → %s (per-symbol params applied).",
            self._strategy.name, new_symbol,
        )

    # ─── Start/Stop commands ───────────────────────────────

    async def _on_start_cmd(self, _topic: str, payload: dict) -> None:
        if self._running:
            return

        values = (payload or {}).get("values", {})
        symbol = values.get("symbol")
        timeframe = values.get(
            "timeframe", config.strategies.strategy_timeframe,
        )
        override_klines = values.get("override_klines")

        if not symbol or self._strategy is None:
            return

        # Apply per-symbol config to the EXISTING instance.
        self._strategy.set_symbol(str(symbol))
        self._strategy.set_timeframe(str(timeframe))
        self._timeframe = str(timeframe)

        await bus.publish(config.protocol.topics["strategy_state"], {
            "warming": True, "running": False, "ready": False,
            "strategy": self._strategy.name, "symbol": symbol,
            "signal_count": 0, "started_at": 0,
        })

        # ─── Klines ───
        if override_klines is not None:
            klines = list(override_klines)
            log.info(
                "StrategyManager: using %d OVERRIDE klines for %s",
                len(klines), symbol,
            )
        else:
            try:
                klines = await asyncio.wait_for(
                    kline_router.fetch(symbol, timeframe, limit=100),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                log.warning("Kline fetch timed out during START")
                klines = []
            except Exception:
                log.exception("Kline fetch failed during START")
                klines = []

        if not klines:
            log.warning(
                "No historical klines for %s — will warm up from live stream.",
                symbol,
            )

        for kp in klines:
            try:
                await self._strategy.on_kline(symbol, kp)
            except Exception:
                pass

        self._running = True
        self._symbol = symbol
        self._signal_count = 0
        self._started_at = int(time.time() * 1000)

        try:
            await self._rehydrate_if_needed(symbol)
        except Exception:
            log.exception("Rehydration failed for %s", symbol)

        await bus.publish(config.protocol.topics["strategy_state"], {
            "warming": False, "running": True, "ready": True,
            "strategy": self._strategy.name, "symbol": symbol,
            "signal_count": 0, "started_at": self._started_at,
        })

    async def _on_stop_cmd(self, _topic: str, _payload: dict) -> None:
        if not self._running:
            return
        self._running = False
        self._cancel_auto_log()

        if self._strategy is not None:
            reset = getattr(self._strategy, "reset", None)
            if callable(reset):
                reset()

        await bus.publish(config.protocol.topics["strategy_state"], {
            "warming": False, "running": False, "ready": True,
            "strategy": self._strategy.name if self._strategy else None,
            "symbol": self._symbol, "signal_count": self._signal_count,
        })
        self._symbol = None

    # ─── Helpers ───────────────────────────────────────────

    def _strategy_has_position(self) -> bool:
        if self._strategy is None:
            return False
        fn = getattr(self._strategy, "has_open_position", None)
        try:
            return bool(fn()) if callable(fn) else False
        except Exception:
            return False

    async def _rehydrate_if_needed(self, symbol: str) -> None:
        if self._strategy is None:
            return
        row = self._db.get_open_trade_for_symbol(symbol)
        if row is None:
            return
        plan_raw = row.get("entry_plan")
        if not plan_raw:
            return
        try:
            plan = json.loads(plan_raw)
        except Exception:
            return
        hydrate = getattr(self._strategy, "hydrate", None)
        if callable(hydrate):
            hydrate(symbol, plan, int(row.get("entry_ts") or 0))

    async def _on_signal_failed(self, _topic: str, payload: dict) -> None:
        if self._strategy is None:
            return
        reset = getattr(self._strategy, "reset_position_state", None)
        if callable(reset):
            reset()

    # ─── Auto-log zones ────────────────────────────────────

    def _start_auto_log(self) -> None:
        self._cancel_auto_log()
        strat_dump = config.strategies.model_dump()
        if not bool(strat_dump.get("auto_log_zones", True)):
            return
        interval = int(strat_dump.get("auto_log_zones_interval_sec", 300))
        if interval <= 0:
            return
        self._auto_log_task = asyncio.create_task(self._auto_log_loop())

    def _cancel_auto_log(self) -> None:
        if self._auto_log_task and not self._auto_log_task.done():
            self._auto_log_task.cancel()
        self._auto_log_task = None

    async def _auto_log_loop(self) -> None:
        try:
            await asyncio.sleep(60)
            while self._running:
                try:
                    await self._on_log_zones("", {})
                except Exception:
                    pass
                await asyncio.sleep(300)
        except asyncio.CancelledError:
            return

    async def _on_log_zones(self, _topic: str, _payload: dict) -> None:
        if self._strategy is None:
            return
        try:
            snapshot = self._strategy.dump_zones(self._symbol or "")
            log.info("──── ZONE SNAPSHOT ────")
            log.info(
                "symbol=%s state=%s",
                snapshot.get("symbol"), snapshot.get("state"),
            )
        except Exception:
            log.exception("dump_zones failed")

    async def _zones_publisher_loop(self) -> None:
        topic = config.protocol.topics["zones_update"]
        while True:
            if self._strategy is not None:
                try:
                    dump_fn = getattr(self._strategy, "dump_zones", None)
                    if callable(dump_fn):
                        snapshot = dump_fn(self._symbol or "")
                        snapshot["symbol"] = self._symbol
                        snapshot["running"] = self._running
                        await bus.publish(topic, snapshot)
                except Exception:
                    pass
            await asyncio.sleep(60.0)

    # ─── Feed fan-out ──────────────────────────────────────

    async def _on_book(self, _topic: str, payload: dict) -> None:
        if not self._running or self._strategy is None:
            return
        if payload.get("symbol") != self._symbol:
            return
        book = payload.get("data") or payload.get("book") or payload
        try:
            signal = await self._strategy.on_book(
                payload["symbol"],
                book.get("bids") or [],
                book.get("asks") or [],
            )
            if signal is not None:
                await self._publish_signal(signal)
        except Exception as exc:
            log.exception("Strategy raised on on_book: %s", exc)

    async def _on_kline(self, _topic: str, payload: dict) -> None:
        if not self._running or self._strategy is None:
            return
        if payload.get("symbol") != self._symbol:
            return
        try:
            signal = await self._strategy.on_kline(payload["symbol"], payload)
            if signal is not None:
                await self._publish_signal(signal)
        except Exception as exc:
            log.exception("Strategy raised on on_kline: %s", exc)

    async def _publish_signal(self, signal: Signal) -> None:
        self._signal_count += 1
        log.info(
            "[PIPELINE 1→2] Signal #%d: %s %s conf=%.2f — %s",
            self._signal_count, signal.action, signal.symbol,
            signal.confidence, signal.reason,
        )
        await bus.publish(
            config.protocol.topics["signal_proposed"], signal.to_dict(),
        )