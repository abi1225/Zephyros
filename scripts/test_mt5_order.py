"""Minimal MT5 order_send test. Run this if the bot reports
(-2, 'Unnamed arguments not allowed')."""
import sys
import MetaTrader5 as mt5

print("Python :", sys.version.split()[0])
print("MT5    :", getattr(mt5, "__version__", "unknown"))
print("Terminal build:", mt5.terminal_info().build if mt5.initialize() else "no terminal")

mt5.initialize()

symbol = "BTCUSD"
tick = mt5.symbol_info_tick(symbol)
if tick is None:
    print(f"no tick for {symbol}")
    sys.exit(1)

request = {
    "action": mt5.TRADE_ACTION_DEAL,
    "symbol": symbol,
    "volume": 0.01,
    "type": mt5.ORDER_TYPE_BUY,
    "price": tick.ask,
    "deviation": 20,
    "magic": 123456,
    "comment": "test",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC,
}

print("--- dict call ---")
r = mt5.order_send(request)
print("result:", r)
print("last_error:", mt5.last_error())

print("--- kwargs call ---")
r = mt5.order_send(**request)
print("result:", r)
print("last_error:", mt5.last_error())

mt5.shutdown()