"""Standalone MT5 test trade.

Places a market BUY, waits, closes it, and logs every step to the
terminal. Uses the same config file as the app so symbol and account
settings stay consistent.

Run from the project root:

    python scripts\\test_trade.py

Optional args:

    --symbol BTCUSDT       canonical id from config
    --volume 0.01          lot size (default: symbol's min_volume or 0.01)
    --hold 5               seconds to hold before closing
    --side BUY             BUY or SELL
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make "app" importable when the script is run from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 package not installed.")
    print("Fix: python -m pip install MetaTrader5")
    sys.exit(1)

from app.core.config import config
from app.core.symbols import registry


# ─── Logging helpers ─────────────────────────────────────────

def hr(char: str = "─", n: int = 62) -> None:
    print(char * n)


def log(tag: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{tag:>7}] {msg}")


def fail(msg: str) -> None:
    log("ERROR", msg)
    sys.exit(1)


# ─── Symbol lookup ───────────────────────────────────────────

def resolve_symbol(canonical_id: str) -> tuple[str, str]:
    """Return (canonical_id, broker_symbol). Exits on failure."""
    sym = registry.get(canonical_id)
    if sym is None:
        fail(f"Symbol {canonical_id!r} not found in config")
    if not sym.mt5:
        fail(f"Symbol {canonical_id!r} has no mt5 mapping in config")
    return canonical_id, sym.mt5


# ─── MT5 helpers ─────────────────────────────────────────────

def init_mt5() -> None:
    cfg = config.execution.mt5
    log("MT5", f"initializing (path={cfg.terminal_path or 'auto'})")

    kwargs = {}
    if cfg.terminal_path:
        kwargs["path"] = cfg.terminal_path

    if not mt5.initialize(**kwargs):
        fail(f"mt5.initialize() failed: {mt5.last_error()}")

    if cfg.login is not None:
        log("MT5", f"logging in as {cfg.login} on {cfg.server}")
        if not mt5.login(cfg.login, password=cfg.password, server=cfg.server):
            mt5.shutdown()
            fail(f"mt5.login() failed: {mt5.last_error()}")

    info = mt5.account_info()
    if info is None:
        mt5.shutdown()
        fail("account_info() returned None")

    hr()
    log("MT5", f"connected  login={info.login}")
    log("MT5", f"           server={info.server}")
    log("MT5", f"           balance={info.balance:.2f} {info.currency}")
    log("MT5", f"           leverage=1:{info.leverage}")
    hr()


def ensure_symbol(broker_symbol: str) -> None:
    info = mt5.symbol_info(broker_symbol)
    if info is None:
        fail(f"Symbol {broker_symbol!r} not found on broker")
    if not info.visible:
        if not mt5.symbol_select(broker_symbol, True):
            fail(f"Could not select symbol {broker_symbol!r}")
        log("MT5", f"selected {broker_symbol} in Market Watch")
    log(
        "MT5",
        f"{broker_symbol}  "
        f"bid={info.bid}  ask={info.ask}  "
        f"spread={info.spread}  "
        f"vol_min={info.volume_min}  vol_step={info.volume_step}",
    )


def detect_filling(broker_symbol: str) -> int:
    info = mt5.symbol_info(broker_symbol)
    mask = int(getattr(info, "filling_mode", 1)) if info else 1
    # Try IOC first, then FOK, then RETURN
    for name, bit, const in (
        ("IOC", 2, mt5.ORDER_FILLING_IOC),
        ("FOK", 1, mt5.ORDER_FILLING_FOK),
        ("RETURN", 4, mt5.ORDER_FILLING_RETURN),
    ):
        if mask & bit:
            log("MT5", f"filling mode: {name} (mask={mask})")
            return const
    log("MT5", f"filling mode: IOC (fallback, mask={mask})")
    return mt5.ORDER_FILLING_IOC


# ─── Order actions ───────────────────────────────────────────

def open_market(broker_symbol: str, side: str, volume: float) -> int:
    tick = mt5.symbol_info_tick(broker_symbol)
    if tick is None:
        fail(f"No tick for {broker_symbol}")

    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if side == "BUY" else tick.bid
    filling = detect_filling(broker_symbol)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": broker_symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": 0.0,
        "tp": 0.0,
        "deviation": config.execution.mt5.deviation_points,
        "magic": config.execution.mt5.magic,
        "comment": "test_trade_open",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    hr("═")
    log("ORDER", f"{side} {volume} {broker_symbol} @ market")
    log("ORDER", f"request: price={price:.2f}  filling={filling}")

    result = mt5.order_send(request)
    if result is None:
        fail(f"order_send returned None: {mt5.last_error()}")

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        fail(
            f"Order rejected  retcode={result.retcode}  "
            f"comment={result.comment!r}"
        )

    log("ORDER", f"FILLED  ticket={result.order}")
    log("ORDER", f"        price={result.price}")
    log("ORDER", f"        volume={result.volume}")
    log("ORDER", f"        retcode={result.retcode} ({result.comment!r})")
    hr("═")

    return int(result.order)


def close_position(ticket: int, broker_symbol: str) -> float:
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        fail(f"Position {ticket} not found")

    p = positions[0]
    tick = mt5.symbol_info_tick(broker_symbol)
    if tick is None:
        fail(f"No tick for {broker_symbol}")

    # Close with opposite market order
    if p.type == mt5.POSITION_TYPE_BUY:
        close_type = mt5.ORDER_TYPE_SELL
        close_price = tick.bid
    else:
        close_type = mt5.ORDER_TYPE_BUY
        close_price = tick.ask

    filling = detect_filling(broker_symbol)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": broker_symbol,
        "volume": float(p.volume),
        "type": close_type,
        "position": int(ticket),
        "price": close_price,
        "deviation": config.execution.mt5.deviation_points,
        "magic": config.execution.mt5.magic,
        "comment": "test_trade_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    hr("═")
    log("CLOSE", f"closing ticket={ticket} with {p.volume} lots")
    log("CLOSE", f"request: price={close_price:.2f}  filling={filling}")

    result = mt5.order_send(request)
    if result is None:
        fail(f"close order_send returned None: {mt5.last_error()}")

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        fail(
            f"Close rejected  retcode={result.retcode}  "
            f"comment={result.comment!r}"
        )

    log("CLOSE", f"FILLED  ticket={result.order}  price={result.price}")
    hr("═")

    # Query P&L from history
    time.sleep(0.5)  # give the broker a moment to settle
    deals = mt5.history_deals_get(position=ticket) or []
    pnl = 0.0
    for d in deals:
        pnl += float(getattr(d, "profit", 0.0) or 0.0)
        pnl += float(getattr(d, "swap", 0.0) or 0.0)
        pnl += float(getattr(d, "commission", 0.0) or 0.0)

    return pnl


# ─── Main ────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone MT5 test trade."
    )
    parser.add_argument(
        "--symbol", default=None,
        help="Canonical symbol id from config (default: first enabled)",
    )
    parser.add_argument(
        "--volume", type=float, default=None,
        help="Lot size (default: symbol's min_volume or 0.01)",
    )
    parser.add_argument(
        "--hold", type=float, default=5.0,
        help="Seconds to hold before closing (default: 5)",
    )
    parser.add_argument(
        "--side", choices=["BUY", "SELL"], default="BUY",
        help="Order side (default: BUY)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Pick the symbol
    canonical = args.symbol
    if canonical is None:
        enabled = registry.enabled_ids()
        if not enabled:
            fail("No enabled symbols in config")
        canonical = enabled[0]
        log("CFG", f"no --symbol passed, using {canonical!r}")

    canonical_id, broker_symbol = resolve_symbol(canonical)
    sym_cfg = registry.require(canonical_id)

    # Pick the volume
    volume = args.volume
    if volume is None:
        volume = sym_cfg.min_volume or 0.01
        log("CFG", f"no --volume passed, using {volume}")

    hr("═")
    log("RUN", f"symbol={canonical_id}  broker={broker_symbol}")
    log("RUN", f"side={args.side}  volume={volume}  hold={args.hold}s")
    hr("═")

    # Connect
    init_mt5()
    ensure_symbol(broker_symbol)

    # Open
    try:
        ticket = open_market(broker_symbol, args.side, volume)
    except SystemExit:
        mt5.shutdown()
        raise
    except Exception as exc:
        mt5.shutdown()
        fail(f"unexpected error opening: {exc!r}")

    # Show position details
    positions = mt5.positions_get(ticket=ticket)
    if positions:
        p = positions[0]
        log("POS", f"ticket={p.ticket}  symbol={p.symbol}")
        log("POS", f"      type={'BUY' if p.type == mt5.POSITION_TYPE_BUY else 'SELL'}")
        log("POS", f"      volume={p.volume}  entry={p.price_open}")
        log("POS", f"      current={p.price_current}  pnl={p.profit:+.2f}")

    # Hold
    log("WAIT", f"holding for {args.hold} seconds...")
    for i in range(int(args.hold), 0, -1):
        positions = mt5.positions_get(ticket=ticket)
        if positions:
            p = positions[0]
            log("WAIT", f"  t-{i}s  pnl={p.profit:+.2f}")
        time.sleep(1)

    # Close
    pnl = close_position(ticket, broker_symbol)

    hr("═")
    log("DONE", f"closed ticket={ticket}")
    log("DONE", f"P&L = {pnl:+.2f} {mt5.account_info().currency}")
    hr("═")

    mt5.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        log("ABORT", "Ctrl+C — shutting down")
        try:
            mt5.shutdown()
        except Exception:
            pass
        sys.exit(130)