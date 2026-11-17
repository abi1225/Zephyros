"""Isolate (-2, 'Unnamed arguments not allowed')."""
from __future__ import annotations
import sys
from concurrent.futures import ThreadPoolExecutor
import MetaTrader5 as mt5

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDm"

def main() -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"Wrapper: {getattr(mt5, '__version__', 'unknown')}")
    if not mt5.initialize():
        print(f"init failed: {mt5.last_error()}"); return 1
    info = mt5.terminal_info()
    print(f"Terminal build: {info.build if info else '?'}")

    if mt5.symbol_info(SYMBOL) is None:
        print(f"no symbol {SYMBOL}"); mt5.shutdown(); return 1

    tick = mt5.symbol_info_tick(SYMBOL)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": 0.01,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "sl": 0.0,
        "tp": 0.0,
        "deviation": 20,
        "magic": 123456,
        "comment": "smoke",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    print("\n--- order_check, main thread ---")
    r = mt5.order_check(req)
    print(f"result={r}")
    print(f"last_error={mt5.last_error()}")

    print("\n--- order_check, executor thread ---")
    with ThreadPoolExecutor(max_workers=1) as ex:
        r = ex.submit(mt5.order_check, req).result(timeout=10)
        print(f"result={r}")
        print(f"last_error={mt5.last_error()}")

    mt5.shutdown()
    return 0

if __name__ == "__main__":
    sys.exit(main())