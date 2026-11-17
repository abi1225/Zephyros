"""UI + DB schemas per strategy.

`STRATEGY_SCHEMAS` drives three things:

  1. The strategy selector dropdown in the Ribbon.
  2. The Debug panel — column layout, formatters, colorizers, legend.
  3. The TelemetryRecorder — routes each `debug.decision` payload
     into the DB table named by `db_table`.
"""
from __future__ import annotations

from typing import Any


STRATEGY_SCHEMAS: dict[str, dict[str, Any]] = {
    # ─── Institutional Tape Reader ────────────────────────
    "tape_reader": {
        "label": "Institutional Tape Reader",
        "db_table": "debug_tape_reader",
        "legend": (
            "<b>Institutional Tape Reader</b><br>"
            "Trades pure Time & Sales (AggTrade) execution data.<br>"
            "Looks for Volume Velocity (sweeps) and CVD Divergence.<br>"
            "Stop Loss: Structural Swing +/- 0.5 ATR.<br>"
            "Trailing: Parabolic SAR activates at 1 ATR."
        ),
        "columns": [
            {"key": "ts", "label": "Time", "width": 75, "fmt": "time", "color": "muted"},
            {"key": "mid", "label": "Price", "width": 80, "fmt": "price"},
            {"key": "state", "label": "State", "width": 60, "fmt": "state", "color": "state"},
            {"key": "cvd", "label": "CVD (5s)", "width": 80, "fmt": "signed_int", "color": "signed"},
            {"key": "velocity", "label": "Vel (vol/s)", "width": 80, "fmt": "raw"},
            {"key": "decision", "label": "Decision", "width": 80, "fmt": "raw", "color": "decision"},
            {"key": "reason", "label": "Reason", "width": None, "fmt": "raw"},
        ],
    },

    # ─── Four Pattern (Order Flow) ────────────────────────
    "four_pattern": {
        "label": "Four Pattern (Order Flow)",
        "db_table": "debug_four_pattern",
        "legend": (
            "<b>Four Pattern Order-Flow</b><br>"
            "Trades SPRING, FAILED_RALLY, FAILED_DEFENSE_UP, FAILED_DEFENSE_DOWN.<br>"
            "Requires L2 and Tape data.<br>"
            "No broker-side SL or TP. Pure zone-to-zone exits."
        ),
        "columns": [
            {"key": "ts", "label": "Time", "width": 75, "fmt": "time", "color": "muted"},
            {"key": "mid", "label": "Price", "width": 80, "fmt": "price"},
            {"key": "state", "label": "State", "width": 100, "fmt": "state", "color": "state"},
            {"key": "pattern", "label": "Pattern", "width": 140, "fmt": "raw"},
            {"key": "decision", "label": "Decision", "width": 80, "fmt": "raw", "color": "decision"},
            {"key": "reason", "label": "Reason", "width": None, "fmt": "raw"},
        ],
    },
}


def get_schema(name: str) -> dict[str, Any]:
    return STRATEGY_SCHEMAS.get(name, STRATEGY_SCHEMAS["tape_reader"])


def get_strategy_db_table(name: str) -> str | None:
    s = STRATEGY_SCHEMAS.get(name)
    return s.get("db_table") if s else None


def strategy_choices() -> list[tuple[str, str]]:
    return [(k, v["label"]) for k, v in STRATEGY_SCHEMAS.items()]


def strategy_names() -> list[str]:
    return list(STRATEGY_SCHEMAS.keys())


def get_supported_symbols(strategy_name: str) -> list[str]:
    """
    No longer enforced. Any strategy can run on any symbol.
    Kept as a stub so existing imports continue to work.
    """
    return []