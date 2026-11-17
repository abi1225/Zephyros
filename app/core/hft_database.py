from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS debug_tape_reader (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    mid         REAL,
    state       TEXT,
    cvd         REAL,
    velocity    REAL,
    decision    TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_dbg_tr_ts   ON debug_tape_reader(ts);
CREATE INDEX IF NOT EXISTS idx_dbg_tr_sym  ON debug_tape_reader(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_dbg_tr_dec  ON debug_tape_reader(decision);
CREATE TABLE IF NOT EXISTS debug_four_pattern (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    mid         REAL,
    state       TEXT,
    pattern     TEXT,
    decision    TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_dbg_fp_ts   ON debug_four_pattern(ts);
CREATE INDEX IF NOT EXISTS idx_dbg_fp_sym  ON debug_four_pattern(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_dbg_fp_dec  ON debug_four_pattern(decision);
"""


_DEBUG_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "debug_tape_reader": [
        ("ts", "ts"), ("symbol", "symbol"),
        ("mid", "mid"), ("state", "state"),
        ("cvd", "cvd"), ("velocity", "velocity"),
        ("decision", "decision"), ("reason", "reason"),
    ],
        "debug_four_pattern": [
        ("ts", "ts"), ("symbol", "symbol"),
        ("mid", "mid"), ("state", "state"),
        ("pattern", "pattern"),
        ("decision", "decision"), ("reason", "reason"),
    ],
}


def _payload_get(payload: dict, path: str) -> Any:
    if "." not in path:
        return payload.get(path)
    cur: Any = payload
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class HFTDatabase:
    def __init__(self, path: str = "data/hft_ai_dataset.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as c:
            c.executescript(SCHEMA)

    # ─── Per-strategy debug telemetry ───
    def log_strategy_telemetry(self, table: str, payload: dict) -> None:
        spec = _DEBUG_COLUMNS.get(table)
        if spec is None:
            log.warning("Unknown strategy debug table: %s", table)
            return
        col_names = [c for c, _ in spec]
        values = tuple(_payload_get(payload, p) for _, p in spec)
        placeholders = ", ".join("?" * len(col_names))
        sql = (
            f"INSERT INTO {table} ({', '.join(col_names)}) "
            f"VALUES ({placeholders})"
        )
        with self._connect() as c:
            c.execute(sql, values)

    @staticmethod
    def known_debug_tables() -> list[str]:
        return list(_DEBUG_COLUMNS.keys())


hft_db = HFTDatabase()