"""Standalone Binance connectivity test. Run with: python scripts\test_binance.py"""
import asyncio
import json

import aiohttp
import websockets


async def main() -> None:
    # 1. REST snapshot
    rest = "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5"
    async with aiohttp.ClientSession() as session:
        async with session.get(rest) as resp:
            print("REST status:", resp.status)
            data = await resp.json()
            print("snapshot bids (first 3):", data["bids"][:3])
            print("snapshot asks (first 3):", data["asks"][:3])
            print("lastUpdateId:", data["lastUpdateId"])

    # 2. WebSocket stream
    url = ("wss://stream.binance.com:9443/stream?streams="
           "btcusdt@depth@100ms/btcusdt@aggTrade")
    print("opening WS:", url)
    async with websockets.connect(url) as ws:
        for i in range(5):
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            stream = data.get("stream", "?")
            payload = data.get("data", {})
            if "@depth" in stream:
                print(f"[{i}] depth  U={payload.get('U')} u={payload.get('u')} "
                      f"bids={len(payload.get('b', []))} asks={len(payload.get('a', []))}")
            else:
                print(f"[{i}] trade  p={payload.get('p')} q={payload.get('q')} "
                      f"m={payload.get('m')}")


if __name__ == "__main__":
    asyncio.run(main())