from __future__ import annotations

import ast
import asyncio
import logging
import sqlite3
import time
import zlib
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from app.core.config import config

log = logging.getLogger(__name__)


def _encode_pairs(pairs) -> bytes:
    """Compress a list of (price, qty) pairs to a BLOB."""
    return zlib.compress(str(pairs).encode("utf-8"), level=6)


def _parse_pairs(raw) -> list[tuple[float, float]]:
    """Decode a stored value. Handles both legacy TEXT rows
    (ast-parsed str) and new BLOB rows (zlib-compressed str)."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = zlib.decompress(raw).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            return []
    try:
        parsed = ast.literal_eval(raw)
        return [(float(p), float(q)) for p, q in parsed]
    except Exception:
        return []


class BaseL2Store(ABC):
    @abstractmethod
    async def save_snapshot(self, timestamp: int, symbol: str, bids: list, asks: list) -> None:
        pass

    @abstractmethod
    async def save_trade(self, timestamp: int, symbol: str, price: float, qty: float, side: str) -> None:
        pass

    @abstractmethod
    async def get_latest_timestamp(self, symbol: str) -> int | None:
        pass

    @abstractmethod
    async def get_recent_snapshots(self, symbol: str, limit: int = 5000) -> list[dict]:
        pass

    @abstractmethod
    async def get_recent_trades(self, symbol: str, since_ts: int = 0, limit: int = 20000) -> list[dict]:
        pass


# ─── Local SQLite ───────────────────────────────────────────

class LocalL2Store(BaseL2Store):
    """SQLite-backed L2 store. Writes go to a worker thread so the
    asyncio loop never blocks. WAL journaling handles concurrent reads.

    Auto-vacuum strategy:
      - Fresh DB: PRAGMA auto_vacuum = INCREMENTAL is set BEFORE
        any DDL, so it takes effect from birth and no VACUUM is
        ever needed.
      - Pre-existing DB (from an older build): the setting was
        NONE, and SQLite ignores auto_vacuum changes on an existing
        file until a full VACUUM. That VACUUM is blocking and slow
        on multi-GB files, so it is opt-in via
        data_pipeline.local.vacuum_on_start.
    """

    def __init__(self) -> None:
        self.db_path = Path(config.data_pipeline.local.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._buffers: dict[str, list[tuple]] = {}
        self._trade_buffers: dict[str, list[tuple]] = {}
        self._last_flush = time.time()
        self._last_janitor = time.time()
        self._flushing = False

        self._bootstrap_fresh_db()
        self._init_schema()

        if self.db_path.exists() and not self._is_fresh:
            self._migrate_auto_vacuum_if_enabled()

    def _bootstrap_fresh_db(self) -> None:
        self._is_fresh = not self.db_path.exists()
        if not self._is_fresh:
            return
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS l2_snapshots (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol  TEXT NOT NULL,
                    ts      INTEGER NOT NULL,
                    bids    BLOB NOT NULL,
                    asks    BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_l2_symbol_ts
                    ON l2_snapshots(symbol, ts);

                CREATE TABLE IF NOT EXISTS l2_trades (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol  TEXT NOT NULL,
                    ts      INTEGER NOT NULL,
                    price   REAL NOT NULL,
                    qty     REAL NOT NULL,
                    side    TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_l2t_symbol_ts
                    ON l2_trades(symbol, ts);
            """)

    def _migrate_auto_vacuum_if_enabled(self) -> None:
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            try:
                mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
                if mode == 2:
                    return
                if not bool(getattr(
                    config.data_pipeline.local, "vacuum_on_start", False,
                )):
                    log.info(
                        "Existing L2 DB with auto_vacuum=NONE. "
                        "Skipping startup VACUUM (set "
                        "data_pipeline.local.vacuum_on_start: true "
                        "in config to run it on the next restart)."
                    )
                    return
                log.info(
                    "vacuum_on_start=true — running one-time VACUUM "
                    "on existing DB (blocking, may take minutes)"
                )
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                conn.commit()
                conn.isolation_level = None
                conn.execute("VACUUM")
                log.info("VACUUM complete — auto_vacuum now INCREMENTAL")
            finally:
                conn.close()
        except Exception as exc:
            log.warning(f"Auto-vacuum migration skipped: {exc}")

    # ─── Write path ─────────────────────────────────────────

    async def save_snapshot(self, timestamp: int, symbol: str, bids: list, asks: list) -> None:
        self._buffers.setdefault(symbol, []).append((
            symbol, timestamp, _encode_pairs(bids), _encode_pairs(asks),
        ))
        await self._maybe_flush()

    async def save_trade(self, timestamp: int, symbol: str, price: float, qty: float, side: str) -> None:
        self._trade_buffers.setdefault(symbol, []).append((
            symbol, timestamp, float(price), float(qty), str(side),
        ))
        await self._maybe_flush()

    async def _maybe_flush(self) -> None:
        if time.time() - self._last_flush < config.data_pipeline.local.flush_interval_sec:
            return
        if self._flushing:
            return

        self._flushing = True
        self._last_flush = time.time()

        try:
            snap_chunks = {sym: buf for sym, buf in self._buffers.items() if buf}
            trade_chunks = {sym: buf for sym, buf in self._trade_buffers.items() if buf}
            for sym in snap_chunks:
                self._buffers[sym] = []
            for sym in trade_chunks:
                self._trade_buffers[sym] = []

            if snap_chunks or trade_chunks:
                await asyncio.to_thread(self._flush_sync, snap_chunks, trade_chunks)
        finally:
            self._flushing = False

    def _flush_sync(self, snap_chunks: dict, trade_chunks: dict) -> None:
        try:
            with self._connect() as conn:
                for rows in snap_chunks.values():
                    conn.executemany(
                        "INSERT INTO l2_snapshots (symbol, ts, bids, asks) "
                        "VALUES (?, ?, ?, ?)",
                        rows,
                    )
                for rows in trade_chunks.values():
                    conn.executemany(
                        "INSERT INTO l2_trades (symbol, ts, price, qty, side) "
                        "VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
        except Exception as exc:
            log.error(f"L2 SQLite flush failed: {exc}")
            return

        if time.time() - self._last_janitor >= config.data_pipeline.local.janitor_interval_sec:
            self._last_janitor = time.time()
            self._run_janitor()

    def _run_janitor(self) -> None:
        cutoff_ms = int(
            (datetime.now() - timedelta(hours=config.data_pipeline.local.retention_hours))
            .timestamp() * 1000
        )
        try:
            with self._connect() as conn:
                d1 = conn.execute(
                    "DELETE FROM l2_snapshots WHERE ts < ?", (cutoff_ms,),
                ).rowcount
                d2 = conn.execute(
                    "DELETE FROM l2_trades WHERE ts < ?", (cutoff_ms,),
                ).rowcount
                try:
                    conn.execute("PRAGMA incremental_vacuum")
                except sqlite3.Error:
                    pass
            if d1 + d2 > 0:
                log.info(f"L2 janitor deleted {d1} snapshots, {d2} trades")
        except Exception as exc:
            log.error(f"L2 janitor failed: {exc}")

    # ─── Read path ──────────────────────────────────────────

    async def get_latest_timestamp(self, symbol: str) -> int | None:
        def _q() -> int | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT ts FROM l2_snapshots "
                    "WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
                return int(row[0]) if row else None
        try:
            return await asyncio.to_thread(_q)
        except Exception as exc:
            log.error(f"L2 read failed: {exc}")
            return None

    async def get_recent_snapshots(self, symbol: str, limit: int = 5000) -> list[dict]:
        def _q() -> list[dict]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, bids, asks FROM l2_snapshots "
                    "WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
                return [
                    {"timestamp": int(r[0]),
                     "bids": _parse_pairs(r[1]),
                     "asks": _parse_pairs(r[2])}
                    for r in reversed(rows)
                ]
        try:
            return await asyncio.to_thread(_q)
        except Exception as exc:
            log.error(f"L2 bulk read failed: {exc}")
            return []

    async def get_sampled_snapshots(
        self, symbol: str, since_ts: int, target_count: int = 1000,
    ) -> list[dict]:
        """Return up to target_count evenly-spaced snapshots in range.

        Uses `id % stride = 0` for O(1) sampling — no row-count scan
        of the full window, no memory spike on multi-hour pulls.
        """
        def _q() -> list[dict]:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM l2_snapshots "
                    "WHERE symbol = ? AND ts >= ?",
                    (symbol, since_ts),
                ).fetchone()
                total = int(row[0]) if row else 0
                if total == 0:
                    return []
                stride = max(1, total // max(target_count, 1))
                rows = conn.execute(
                    "SELECT ts, bids, asks FROM l2_snapshots "
                    "WHERE symbol = ? AND ts >= ? AND (id % ?) = 0 "
                    "ORDER BY ts",
                    (symbol, since_ts, stride),
                ).fetchall()
                return [
                    {"timestamp": int(r[0]),
                     "bids": _parse_pairs(r[1]),
                     "asks": _parse_pairs(r[2])}
                    for r in rows
                ]
        try:
            return await asyncio.to_thread(_q)
        except Exception as exc:
            log.error(f"L2 sampled read failed: {exc}")
            return []

    async def get_recent_trades(self, symbol: str, since_ts: int = 0, limit: int = 20000) -> list[dict]:
        def _q() -> list[dict]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, price, qty, side FROM l2_trades "
                    "WHERE symbol = ? AND ts >= ? ORDER BY ts ASC LIMIT ?",
                    (symbol, since_ts, limit),
                ).fetchall()
                return [
                    {"timestamp": int(r[0]), "price": float(r[1]),
                     "qty": float(r[2]), "side": str(r[3])}
                    for r in rows
                ]
        try:
            return await asyncio.to_thread(_q)
        except Exception as exc:
            log.error(f"L2 trade read failed: {exc}")
            return []


# ─── Remote VPS (read-only) ────────────────────────────────

class RemoteL2Reader(BaseL2Store):
    """Read-only client. VPS records 24/7; bot only reads."""

    def __init__(self) -> None:
        self.api_url = config.data_pipeline.vps.api_url.rstrip("/")
        self.headers = {"X-API-Key": config.data_pipeline.vps.api_key}
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session

    async def save_snapshot(self, timestamp: int, symbol: str, bids: list, asks: list) -> None:
        return

    async def save_trade(self, timestamp: int, symbol: str, price: float, qty: float, side: str) -> None:
        return

    async def get_latest_timestamp(self, symbol: str) -> int | None:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.api_url}/latest_timestamp",
                params={"symbol": symbol},
                timeout=3,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("timestamp")
        except Exception:
            return None
        return None

    async def get_recent_snapshots(self, symbol: str, limit: int = 5000) -> list[dict]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.api_url}/snapshots",
                params={"symbol": symbol, "limit": limit},
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("snapshots", [])
        except Exception:
            return []
        return []

    async def get_recent_trades(self, symbol: str, since_ts: int = 0, limit: int = 20000) -> list[dict]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.api_url}/trades",
                params={"symbol": symbol, "since_ts": since_ts, "limit": limit},
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("trades", [])
        except Exception:
            return []
        return []


def get_l2_store() -> BaseL2Store | None:
    mode = config.data_pipeline.storage_mode
    if mode == "vps":
        log.info("L2 pipeline: VPS mode (bot reads, VPS records)")
        return RemoteL2Reader()
    if mode == "local":
        log.info("L2 pipeline: LOCAL mode (bot reads and records)")
        return LocalL2Store()
    log.warning("L2 pipeline: DISABLED")
    return None