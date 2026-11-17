from __future__ import annotations

import logging

from app.core.config import config
from app.core.event_bus import bus
from app.core.hft_database import hft_db
from app.core.module import BaseModule
from app.modules.strategy.schemas import get_strategy_db_table

log = logging.getLogger(__name__)


class TelemetryRecorder(BaseModule):
    """Persists every `debug.decision` event to SQLite for offline analysis.

    Routes each event into the table named by the currently active
    strategy's schema (`STRATEGY_SCHEMAS[name]['db_table']`). On boot
    it primes itself from `config.strategies.active`; thereafter it
    tracks the `strategy_changed` topic. If no strategy is known yet
    (e.g. schema lookup fails), it falls back to the legacy
    `debug_telemetry` table so no data is lost.
    """

    name = "data.telemetry_recorder"

    def __init__(self) -> None:
        self._debug_topic = config.protocol.topics.get(
            "debug_decision", "debug.decision"
        )
        self._strategy_topic = config.protocol.topics.get(
            "strategy_changed", "strategy_changed"
        )

        self._active_strategy: str = ""
        self._active_table: str = ""
        self._count: int = 0
        self._error_logged: bool = False

        self._prime_from_config()

    # ─── Bootstrap ──────────────────────────────────────────

    def _prime_from_config(self) -> None:
        """Set initial routing from `config.strategies.active`.

        Runs before `start()` so the first debug events that arrive
        (which could land before StrategyManager.start() publishes
        `strategy_changed`) are routed correctly.
        """
        name = str(config.strategies.active or "").strip()
        if not name:
            return
        table = get_strategy_db_table(name) or ""
        if not table:
            log.warning(
                "TelemetryRecorder: no db_table for active strategy %r — "
                "falling back to legacy debug_telemetry",
                name,
            )
            return
        self._active_strategy = name
        self._active_table = table
        log.info(
            "TelemetryRecorder primed → %s (%s)", name, table,
        )

    # ─── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        bus.subscribe(self._debug_topic, self._on_decision)
        bus.subscribe(self._strategy_topic, self._on_strategy_changed)
        target = self._active_table or "debug_telemetry (legacy)"
        log.info(
            "TelemetryRecorder started — listening on %s → %s",
            self._debug_topic, target,
        )

    async def stop(self) -> None:
        for topic, handler in (
            (self._debug_topic, self._on_decision),
            (self._strategy_topic, self._on_strategy_changed),
        ):
            try:
                bus.unsubscribe(topic, handler)
            except Exception:
                pass
        log.info(
            "TelemetryRecorder stopped (%d events logged, last=%s)",
            self._count, self._active_strategy or "—",
        )

    # ─── Handlers ───────────────────────────────────────────

    async def _on_strategy_changed(self, _topic: str, payload: dict) -> None:
        name = str((payload or {}).get("strategy", "")).strip()
        if not name:
            return

        table = str((payload or {}).get("db_table", "")).strip()
        if not table:
            table = get_strategy_db_table(name) or ""

        if name == self._active_strategy and table == self._active_table:
            return

        self._active_strategy = name
        self._active_table = table

        if table:
            log.info("TelemetryRecorder switched → %s (%s)", name, table)
        else:
            log.warning(
                "TelemetryRecorder switched → %s (no db_table — using "
                "legacy debug_telemetry)", name,
            )

    async def _on_decision(self, _topic: str, payload: dict) -> None:
        try:
            if self._active_table:
                hft_db.log_strategy_telemetry(self._active_table, payload)
            else:
                hft_db.log_telemetry(payload)
            self._count += 1
        except Exception as exc:
            if not self._error_logged:
                log.error(
                    "Telemetry write failed (strategy=%r table=%r): %s",
                    self._active_strategy or "—",
                    self._active_table or "debug_telemetry",
                    exc,
                )
                self._error_logged = True