from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT,
    entry_order_id  INTEGER,
    exit_order_id   INTEGER,
    symbol          TEXT NOT NULL,
    broker_symbol   TEXT,
    side            TEXT NOT NULL,
    volume          REAL NOT NULL,
    entry_price     REAL,
    exit_price      REAL,
    entry_ts        INTEGER NOT NULL,
    exit_ts         INTEGER,
    pnl             REAL,
    pnl_pct         REAL,
    exit_reason     TEXT,
    strategy        TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    notes           TEXT,
    entry_plan      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts ON trades(entry_ts);
CREATE INDEX IF NOT EXISTS idx_trades_exit_ts  ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ticket   ON trades(entry_order_id);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT UNIQUE,
    ts              INTEGER NOT NULL,
    strategy        TEXT,
    symbol          TEXT,
    action          TEXT,
    confidence      REAL,
    reason          TEXT,
    approved        INTEGER NOT NULL,
    reject_reason   TEXT,
    metadata        TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts       ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_symbol   ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_approved ON signals(approved);
"""


class TradesDB:
    """SQLite wrapper for trade and signal history."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as c:
            c.execute("PRAGMA journal_mode = WAL;")
            c.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Bring older DBs up to the current schema. Idempotent."""
        with self._connect() as c:
            cols = {
                row["name"]
                for row in c.execute("PRAGMA table_info(trades)").fetchall()
            }
            if "entry_plan" not in cols:
                log.info("Migrating trades table: adding entry_plan column")
                c.execute("ALTER TABLE trades ADD COLUMN entry_plan TEXT")

    # ─── Signals ────────────────────────────────────────────

    def insert_signal(
        self,
        signal_id: str,
        ts: int,
        strategy: str,
        symbol: str,
        action: str,
        confidence: float,
        reason: str,
        approved: bool,
        reject_reason: str | None,
        metadata: dict | None,
    ) -> None:
        with self._connect() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO signals
                (signal_id, ts, strategy, symbol, action, confidence,
                 reason, approved, reject_reason, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id, ts, strategy, symbol, action, confidence,
                    reason, 1 if approved else 0, reject_reason,
                    json.dumps(metadata or {}),
                ),
            )

    # ─── Trades ─────────────────────────────────────────────

    def open_trade(
        self,
        signal_id: str,
        entry_order_id: int,
        symbol: str,
        broker_symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        entry_ts: int,
        strategy: str,
        status: str = "open",
        notes: str | None = None,
        entry_plan: dict | None = None,
    ) -> int:
        with self._connect() as c:
            cur = c.execute(
                """
                INSERT INTO trades
                (signal_id, entry_order_id, symbol, broker_symbol, side,
                 volume, entry_price, entry_ts, strategy, status, notes,
                 entry_plan)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id, entry_order_id, symbol, broker_symbol, side,
                    volume, entry_price, entry_ts, strategy, status, notes,
                    json.dumps(entry_plan) if entry_plan else None,
                ),
            )
            return int(cur.lastrowid or 0)

    def close_trade(
        self,
        entry_order_id: int,
        exit_order_id: int | None,
        exit_price: float | None,
        exit_ts: int,
        pnl: float | None,
        pnl_pct: float | None,
        exit_reason: str | None,
    ) -> None:
        with self._connect() as c:
            c.execute(
                """
                UPDATE trades
                SET exit_order_id = ?, exit_price = ?, exit_ts = ?,
                    pnl = ?, pnl_pct = ?, exit_reason = ?, status = 'closed'
                WHERE entry_order_id = ? AND status IN ('open', 'orphaned')
                """,
                (
                    exit_order_id, exit_price, exit_ts,
                    pnl, pnl_pct, exit_reason, entry_order_id,
                ),
            )

    def mark_lost(self, entry_order_id: int, notes: str) -> None:
        with self._connect() as c:
            c.execute(
                """
                UPDATE trades
                SET status = 'lost', notes = ?
                WHERE entry_order_id = ?
                """,
                (notes, entry_order_id),
            )

    # ─── Queries ────────────────────────────────────────────

    def get_open_trades(self) -> list[dict]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE status IN ('open', 'orphaned')"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trade_for_symbol(self, symbol: str) -> dict | None:
        """Most recent open/orphaned trade on `symbol`, if any."""
        with self._connect() as c:
            row = c.execute(
                """
                SELECT * FROM trades
                WHERE symbol = ? AND status IN ('open', 'orphaned')
                ORDER BY entry_ts DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def get_trade_by_ticket(self, ticket: int) -> dict | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT * FROM trades WHERE entry_order_id = ?",
                (ticket,),
            ).fetchone()
        return dict(row) if row else None

    def get_trades_since(self, ts_ms: int) -> list[dict]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE entry_ts >= ? ORDER BY entry_ts",
                (ts_ms,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_trades_range(
        self, from_ms: int, to_ms: int, limit: int = 5000,
    ) -> list[dict]:
        with self._connect() as c:
            rows = c.execute(
                """
                SELECT * FROM trades
                WHERE status = 'closed'
                  AND exit_ts IS NOT NULL
                  AND exit_ts >= ? AND exit_ts <= ?
                ORDER BY exit_ts DESC
                LIMIT ?
                """,
                (from_ms, to_ms, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_rejections(self, limit: int = 50) -> list[dict]:
        with self._connect() as c:
            rows = c.execute(
                """
                SELECT * FROM signals
                WHERE approved = 0
                ORDER BY ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def realized_pnl_since(self, ts_ms: int) -> float:
        with self._connect() as c:
            row = c.execute(
                """
                SELECT COALESCE(SUM(pnl), 0.0) AS total
                FROM trades
                WHERE status = 'closed' AND exit_ts >= ?
                """,
                (ts_ms,),
            ).fetchone()
        return float(row["total"]) if row else 0.0

    def trade_counts_since(self, ts_ms: int) -> dict[str, int]:
        with self._connect() as c:
            row = c.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses
                FROM trades
                WHERE status = 'closed' AND exit_ts >= ?
                """,
                (ts_ms,),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
        }


_db: TradesDB | None = None


def get_db() -> TradesDB:
    global _db
    if _db is None:
        from app.core.config import config
        _db = TradesDB(config.app.database_path)
    return _db