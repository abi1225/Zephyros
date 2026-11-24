"""Does MT5 accept init + calls on the SAME worker thread?"""
from __future__ import annotations
import threading
from concurrent.futures import ThreadPoolExecutor
import MetaTrader5 as mt5

SYMBOL = "BTCUSDm"


def worker(worker_id: int) -> None:
    print(f"[worker {worker_id}] thread id={threading.get_ident()}")
    if not mt5.initialize():
        print(f"[worker {worker_id}] init failed: {mt5.last_error()}")
        return
    print(f"[worker {worker_id}] init ok, thread id={threading.get_ident()}")

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
    r = mt5.order_check(req)
    print(f"[worker {worker_id}] order_check result={r}")
    print(f"[worker {worker_id}] last_error={mt5.last_error()}")
    # Do NOT shutdown here — we want to see if the thread persists


def main() -> None:
    print(f"main thread id={threading.get_ident()}")

    # Reuse the same executor for two consecutive tasks. If thread
    # reuse works, worker_id prints the same tid twice.
    with ThreadPoolExecutor(max_workers=1) as ex:
        for i in range(2):
            ex.submit(worker, i).result(timeout=15)

    mt5.shutdown()


if __name__ == "__main__":
    main()