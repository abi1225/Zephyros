"""Zephyros VPS Recorder.

Standalone service that:
  1. Connects to Binance WS (depth + aggTrade) for configured symbols
  2. Reconstructs the order book locally
  3. Writes snapshots + trades to SQLite (batched, threaded)
  4. Serves read-only HTTP endpoints for the bot's warm-up

Deploy: python vps_server.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import websockets
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import uvicorn


# ─── Configuration ──────────────────────────────────────────

API_KEY = "super_secret_key_change_me"     # match config/local.yaml
DB_PATH = Path("vps_l2_history.db")
RETENTION_HOURS = 24
FLUSH_INTERVAL_SEC = 5
JANITOR_INTERVAL_SEC = 300

BINANCE_WS = "wss://stream.binance.com:9443"
BINANCE_REST = "https://api.binance.com"

# Symbols to record. Add more as needed.
SYMBOLS = {
    "BTCUSDT": "btcusdt",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("vps")


# ─── SQLite ─────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_schema() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS l2_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, ts INTEGER NOT NULL,
                bids TEXT NOT NULL, asks TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_l2_symbol_ts
                ON l2_snapshots(symbol, ts);

            CREATE TABLE IF NOT EXISTS l2_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, ts INTEGER NOT NULL,
                price REAL NOT NULL, qty REAL NOT NULL, side TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_l2t_symbol_ts
                ON l2_trades(symbol, ts);
        """)


# ─── Write buffers (thread-safe via lock) ───────────────────

_buffers_lock = threading.Lock()
_snap_buffers: dict[str, list[tuple]] = {}
_trade_buffers: dict[str, list[tuple]] = {}
_last_flush = time.time()
_last_janitor = time.time()


def buffer_snapshot(symbol: str, ts: int, bids: list, asks: list) -> None:
    with _buffers_lock:
        _snap_buffers.setdefault(symbol, []).append(
            (symbol, ts, str(bids), str(asks))
        )


def buffer_trade(symbol: str, ts: int, price: float, qty: float, side: str) -> None:
    with _buffers_lock:
        _trade_buffers.setdefault(symbol, []).append(
            (symbol, ts, price, qty, side)
        )


def flush_sync() -> None:
    global _last_flush, _last_janitor

    with _buffers_lock:
        snap_chunks = {s: b for s, b in _snap_buffers.items() if b}
        trade_chunks = {s: b for s, b in _trade_buffers.items() if b}
        for s in snap_chunks:
            _snap_buffers[s] = []
        for s in trade_chunks:
            _trade_buffers[s] = []

    if not snap_chunks and not trade_chunks:
        return

    try:
        with get_conn() as conn:
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
        log.info(
            f"[flush] {sum(len(r) for r in snap_chunks.values())} snapshots, "
            f"{sum(len(r) for r in trade_chunks.values())} trades"
        )
    except Exception as exc:
        log.error(f"[flush] failed: {exc}")

    _last_flush = time.time()

    if time.time() - _last_janitor >= JANITOR_INTERVAL_SEC:
        _last_janitor = time.time()
        janitor_sync()


def janitor_sync() -> None:
    cutoff = int(
        (datetime.utcnow() - timedelta(hours=RETENTION_HOURS)).timestamp() * 1000
    )
    try:
        with get_conn() as conn:
            d1 = conn.execute(
                "DELETE FROM l2_snapshots WHERE ts < ?", (cutoff,)
            ).rowcount
            d2 = conn.execute(
                "DELETE FROM l2_trades WHERE ts < ?", (cutoff,)
            ).rowcount
        log.info(f"[janitor] deleted {d1} snapshots, {d2} trades")
    except Exception as exc:
        log.error(f"[janitor] failed: {exc}")


async def flush_loop() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SEC)
        await asyncio.to_thread(flush_sync)


# ─── Binance book reconstruction ────────────────────────────

class BookState:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id = 0
        self.ready = False
        self.first_event = False
        self.buffer: list[dict] = []


async def fetch_snapshot(
    session: aiohttp.ClientSession,
    book: BookState,
    binance_symbol: str,
) -> None:
    url = f"{BINANCE_REST}/api/v3/depth?symbol={binance_symbol}&limit=1000"
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:
        log.warning(f"[{book.symbol}] snapshot failed: {exc}")
        return

    book.bids = {float(p): float(q) for p, q in data["bids"]}
    book.asks = {float(p): float(q) for p, q in data["asks"]}
    book.last_update_id = int(data["lastUpdateId"])
    book.ready = False
    book.first_event = False

    buffered = book.buffer
    book.buffer = []

    log.info(
        f"[{book.symbol}] snapshot: id={book.last_update_id} "
        f"bids={len(book.bids)} asks={len(book.asks)} buffered={len(buffered)}"
    )

    for evt in buffered:
        apply_depth(book, evt)


def apply_depth(book: BookState, event: dict) -> None:
    U = int(event["U"])
    u = int(event["u"])

    if not book.first_event:
        if u <= book.last_update_id:
            return
        book.first_event = True
        book.ready = True
        book.last_update_id = u
    else:
        if U > book.last_update_id + 1:
            log.warning(
                f"[{book.symbol}] gap detected "
                f"(expected={book.last_update_id + 1} got={U}) — resync"
            )
            book.ready = False
            book.first_event = False
            book.last_update_id = 0
            book.buffer = [event]
            asyncio.create_task(resync(book))
            return
        book.last_update_id = u

    for p, q in event.get("b", []):
        price, qty = float(p), float(q)
        if qty == 0:
            book.bids.pop(price, None)
        else:
            book.bids[price] = qty
    for p, q in event.get("a", []):
        price, qty = float(p), float(q)
        if qty == 0:
            book.asks.pop(price, None)
        else:
            book.asks[price] = qty

    if book.ready:
        publish_book(book)


def publish_book(book: BookState) -> None:
    bids = sorted(book.bids.items(), key=lambda kv: -kv[0])[:1000]
    asks = sorted(book.asks.items(), key=lambda kv: kv[0])[:1000]
    ts = int(time.time() * 1000)
    buffer_snapshot(
        book.symbol,
        ts,
        [[p, q] for p, q in bids],
        [[p, q] for p, q in asks],
    )


async def resync(book: BookState) -> None:
    async with aiohttp.ClientSession() as session:
        await fetch_snapshot(session, book, SYMBOLS[book.symbol])


# ─── Binance WS workers ─────────────────────────────────────

async def run_symbol(symbol: str, binance_symbol: str) -> None:
    book = BookState(symbol)
    lower = binance_symbol.lower()
    url = (
        f"{BINANCE_WS}/stream?streams="
        f"{lower}@depth@100ms/{lower}@aggTrade"
    )

    while True:
        try:
            log.info(f"[{symbol}] connecting {url}")
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20,
                close_timeout=5, max_size=2 ** 22,
            ) as ws:
                book.buffer = []
                book.ready = False
                book.first_event = False
                book.last_update_id = 0

                async with aiohttp.ClientSession() as http:
                    snap_task = asyncio.create_task(
                        fetch_snapshot(http, book, binance_symbol)
                    )
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            stream = msg.get("stream", "")
                            data = msg.get("data", msg)

                            if "@depth" in stream:
                                if book.last_update_id == 0:
                                    book.buffer.append(data)
                                else:
                                    apply_depth(book, data)
                            elif "@aggTrade" in stream:
                                handle_trade(symbol, data)
                    finally:
                        if not snap_task.done():
                            snap_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await snap_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"[{symbol}] error: {exc} — reconnecting in 5s")
            await asyncio.sleep(5)


def handle_trade(symbol: str, event: dict) -> None:
    try:
        price = float(event["p"])
        qty = float(event["q"])
        is_buyer_maker = bool(event["m"])
        ts = int(event["T"])
    except (KeyError, TypeError, ValueError):
        return
    side = "SELL" if is_buyer_maker else "BUY"
    buffer_trade(symbol, ts, price, qty, side)


# ─── FastAPI ────────────────────────────────────────────────

app = FastAPI(title="Zephyros VPS Recorder")


@app.get("/latest_timestamp")
async def latest_ts(symbol: str, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API Key")
    def _q():
        with get_conn() as conn:
            row = conn.execute(
                "SELECT ts FROM l2_snapshots WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            return int(row[0]) if row else None
    return {"timestamp": await asyncio.to_thread(_q)}


@app.get("/snapshots")
async def get_snapshots(
    symbol: str, limit: int = 5000, x_api_key: str = Header(None),
):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API Key")
    if not (1 <= limit <= 50_000):
        raise HTTPException(400, "limit must be 1..50000")
    def _q():
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT ts, bids, asks FROM l2_snapshots "
                "WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
            return [
                {"timestamp": int(r[0]), "bids": r[1], "asks": r[2]}
                for r in reversed(rows)
            ]
    return {"snapshots": await asyncio.to_thread(_q)}


@app.get("/trades")
async def get_trades(
    symbol: str, since_ts: int = 0, limit: int = 20000,
    x_api_key: str = Header(None),
):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API Key")
    if not (1 <= limit <= 100_000):
        raise HTTPException(400, "limit must be 1..100000")
    def _q():
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT ts, price, qty, side FROM l2_trades "
                "WHERE symbol=? AND ts>=? ORDER BY ts ASC LIMIT ?",
                (symbol, since_ts, limit),
            ).fetchall()
            return [
                {"timestamp": int(r[0]), "price": float(r[1]),
                 "qty": float(r[2]), "side": str(r[3])}
                for r in rows
            ]
    return {"trades": await asyncio.to_thread(_q)}


@app.get("/stats")
async def stats(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API Key")
    def _q():
        with get_conn() as conn:
            snap = conn.execute(
                "SELECT symbol, COUNT(*), MIN(ts), MAX(ts) "
                "FROM l2_snapshots GROUP BY symbol"
            ).fetchall()
            trade = conn.execute(
                "SELECT symbol, COUNT(*), MIN(ts), MAX(ts) "
                "FROM l2_trades GROUP BY symbol"
            ).fetchall()
            return {
                "snapshots": [
                    {"symbol": r[0], "rows": r[1], "oldest": r[2], "newest": r[3]}
                    for r in snap
                ],
                "trades": [
                    {"symbol": r[0], "rows": r[1], "oldest": r[2], "newest": r[3]}
                    for r in trade
                ],
            }
    return await asyncio.to_thread(_q)


@app.get("/health")
async def health():
    return {"status": "ok", "ts": int(time.time() * 1000)}


# ─── Main ───────────────────────────────────────────────────

async def main() -> None:
    init_schema()
    log.info(f"VPS recorder starting — DB: {DB_PATH}")

    flush_task = asyncio.create_task(flush_loop())
    symbol_tasks = [
        asyncio.create_task(run_symbol(sym, bs))
        for sym, bs in SYMBOLS.items()
    ]

    config = uvicorn.Config(
        app, host="0.0.0.0", port=8000, log_level="info",
    )
    server = uvicorn.Server(config)

    try:
        await asyncio.gather(server.serve(), *symbol_tasks)
    finally:
        flush_task.cancel()
        for t in symbol_tasks:
            t.cancel()
        await asyncio.to_thread(flush_sync)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")