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

    async def start(self) -> None:
        bus.subscribe(config.protocol.topics["cmd_start_bot"], self._on_start_cmd)
        bus.subscribe(config.protocol.topics["cmd_stop_bot"], self._on_stop_cmd)
        bus.subscribe(config.protocol.topics["cmd_log_zones"], self._on_log_zones)
        bus.subscribe(
            config.protocol.topics.get("cmd_select_strategy", "cmd_select_strategy"),
            self._on_select_strategy,
        )
        bus.subscribe(
            config.protocol.topics.get("signal_failed", "signal_failed"),
            self._on_signal_failed,
        )
        bus.subscribe(
            config.protocol.topics.get("ui_symbol_changed", "ui_symbol_changed"),
            self._on_symbol_changed,
        )
        bus.subscribe(config.protocol.topics["feed_depth"], self._on_book)
        bus.subscribe(config.protocol.topics["feed_kline"], self._on_kline)

        asyncio.create_task(self._zones_publisher_loop())
        self._load_active_strategy()

        await bus.publish(config.protocol.topics["strategy_state"], {
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

    # ─── Config merging ────────────────────────────────────────

    def _merge_cfg_for_symbol(self, strategy_name: str, symbol: str) -> dict[str, Any]:
        """Merge base strategy config with per-symbol overrides.

        Base config:      strategies.<strategy>.*
        Per-symbol block: strategies.<strategy>.per_symbol.<symbol>.*
        Override wins on key collision. Undefined overrides fall back to base.
        """
        strat_dump = config.strategies.model_dump()
        base: dict[str, Any] = dict(strat_dump.get(strategy_name, {}) or {})

        # Pop per_symbol out — it must not be passed to the strategy constructor
        per_symbol = base.pop("per_symbol", {}) or {}
        override = per_symbol.get(symbol, {}) or {}

        merged = {**base, **override}

        # Fold in top-level defaults if not present
        for key in ("cooldown_ms", "min_confidence"):
            if key in strat_dump and key not in merged:
                merged[key] = strat_dump[key]

        if override:
            log.info(
                f"StrategyManager: merged {len(override)} per-symbol override(s) "
                f"for {strategy_name} @ {symbol}"
            )
        return merged

    # ─── Strategy load / swap ─────────────────────────────────

    def _load_active_strategy(self) -> None:
        self._swap_strategy(config.strategies.active)

    def _swap_strategy(self, name: str) -> None:
        """Create a strategy with base config (no symbol yet)."""
        klass = STRATEGY_REGISTRY.get(name)
        if klass is None:
            raise KeyError(f"Unknown strategy: {name!r}")

        strat_dump = config.strategies.model_dump()
        cfg: dict = dict(strat_dump.get(name, {}) or {})
        cfg.pop("per_symbol", None)  # drop before construction
        for key in ("cooldown_ms", "min_confidence"):
            if key in strat_dump and key not in cfg:
                cfg[key] = strat_dump[key]

        self._strategy = klass(cfg)
        self._symbol = None
        log.info("Loaded strategy %r with base config.", name)

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

    # ─── Symbol change handler ─────────────────────────────────

    async def _on_symbol_changed(self, _topic: str, payload: dict) -> None:
        new_symbol = str((payload or {}).get("symbol", "")).strip()
        if not new_symbol or not self._running:
            return
        if new_symbol == self._symbol:
            return

        msg = (
            f"Symbol changed to {new_symbol}. Stopping strategy to prevent "
            f"mixing data streams. Please press START again."
        )
        log.warning(msg)
        await bus.publish(
            config.protocol.topics["log_emit"],
            {"level": "warn", "module": self.name, "message": msg},
        )
        await self._on_stop_cmd("", {})

    # ─── Start/Stop commands ───────────────────────────────────

    async def _on_start_cmd(self, _topic: str, payload: dict) -> None:
        if self._running:
            return

        values = (payload or {}).get("values", {})
        symbol = values.get("symbol")
        timeframe = values.get("timeframe", config.strategies.strategy_timeframe)
        override_klines = values.get("override_klines")

        if not symbol or self._strategy is None:
            return

        strategy_name = self._strategy.name

        # ─── Dispose old instance and re-create with merged config ───
        try:
            self._strategy.dispose()
        except Exception:
            log.exception("Strategy dispose() failed on restart")

        merged_cfg = self._merge_cfg_for_symbol(strategy_name, symbol)
        klass = STRATEGY_REGISTRY.get(strategy_name)
        if klass is None:
            log.error(f"Strategy {strategy_name} missing from registry")
            self._strategy = None
            return

        self._strategy = klass(merged_cfg)
        log.info(
            f"Strategy {strategy_name} re-instantiated for {symbol} "
            f"(base + per_symbol overrides)"
        )

        # ─── Apply runtime config ───
        self._timeframe = timeframe

        set_symbol = getattr(self._strategy, "set_symbol", None)
        if callable(set_symbol):
            set_symbol(symbol)

        set_timeframe = getattr(self._strategy, "set_timeframe", None)
        if callable(set_timeframe):
            set_timeframe(timeframe)

        reset_symbol = getattr(self._strategy, "reset_symbol", None)
        if callable(reset_symbol):
            reset_symbol(symbol)

        await bus.publish(config.protocol.topics["strategy_state"], {
            "warming": True, "running": False, "ready": False,
            "strategy": self._strategy.name, "symbol": symbol,
            "signal_count": 0, "started_at": 0,
        })

        # ─── Klines ───
        if override_klines is not None:
            klines = list(override_klines)
            log.info(
                f"StrategyManager: using {len(klines)} OVERRIDE klines for {symbol} "
                f"(KlineRouter bypassed)"
            )
        else:
            try:
                klines = await asyncio.wait_for(
                    kline_router.fetch(symbol, timeframe, limit=100),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                log.warning("Kline fetch timed out during START command")
                klines = []
            except Exception:
                log.exception("Kline fetch failed during START")
                klines = []

        if not klines:
            log.warning(
                f"No historical klines for {symbol}. "
                f"Will warm up from live stream."
            )

        for kline_payload in klines:
            try:
                await self._strategy.on_kline(symbol, kline_payload)
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

    # ─── Auto-log zones ────────────────────────────────────────

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
            log.info(f"symbol={snapshot.get('symbol')} state={snapshot.get('state')}")
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

    # ─── Feed fan-out ──────────────────────────────────────────

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
            f"[PIPELINE 1→2] Signal #{self._signal_count}: "
            f"{signal.action} {signal.symbol} conf={signal.confidence:.2f} "
            f"— {signal.reason}"
        )
        await bus.publish(
            config.protocol.topics["signal_proposed"], signal.to_dict(),
        )