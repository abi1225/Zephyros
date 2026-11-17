from __future__ import annotations

import logging
import random
import time
from typing import Any

import aiohttp

from app.core.config import config
from app.core.symbols import registry

log = logging.getLogger(__name__)


class KlineRouter:
    """Fetches historical klines from the correct data source."""

    async def fetch(self, symbol_id: str, timeframe: str, limit: int = 100) -> list[dict]:
        sym = registry.get(symbol_id)
        if not sym or not sym.enabled:
            return []

        if sym.source == "binance" and sym.binance:
            return await self._fetch_binance(symbol_id, sym.binance, timeframe, limit)
        elif sym.source == "ctrader" and sym.ctrader:
            return await self._fetch_ctrader(symbol_id, sym.ctrader, timeframe, limit)
        
        return []

    async def _fetch_binance(self, symbol_id: str, binance_sym: str, timeframe: str, limit: int) -> list[dict]:
        rest = config.feeds.binance.rest_base.rstrip("/")
        url = f"{rest}/api/v3/klines?symbol={binance_sym}&interval={timeframe}&limit={limit}"

        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        log.warning(f"Binance Kline fetch failed: {resp.status}")
                        return []
                    data = await resp.json()
        except Exception as exc:
            log.warning(f"Binance Kline fetch error: {exc}")
            return []

        now_ms = int(time.time() * 1000)
        payloads = []
        for k in data:
            try:
                if int(k[6]) >= now_ms:
                    continue
                payloads.append({
                    "symbol": symbol_id,
                    "interval": timeframe,
                    "kline": {
                        "t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                        "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
                        "closed": True,
                    },
                })
            except (IndexError, TypeError, ValueError):
                continue
        return payloads

    async def _fetch_ctrader(self, symbol_id: str, ctrader_sym: str, timeframe: str, limit: int) -> list[dict]:
        """
        cTrader does not expose a public REST API for historical klines.
        When synthetic mode is on, generate synthetic klines so the strategy
        can warm up its ATR and detect levels for testing.
        """
        if getattr(config, "synthetic", None) and config.synthetic.enabled:
            if config.synthetic.symbol == symbol_id:
                return self._synthetic_klines(symbol_id, timeframe, limit)

        log.warning(
            f"cTrader Kline fetch not implemented via REST for {ctrader_sym}. "
            f"Relying on live buffer."
        )
        return []

    def _synthetic_klines(self, symbol_id: str, timeframe: str, limit: int) -> list[dict]:
        base_price = config.synthetic.base_price
        now = int(time.time() * 1000)
        base_t = now - (limit * 60 * 1000)
        payloads = []

        for i in range(limit):
            t = base_t + (i * 60 * 1000)

            # Last 5 candles: force tight cluster exactly at base_price.
            # This guarantees the level detection lands on base_price.
            if i > limit - 5:
                o = base_price - 0.05
                c = base_price
                h = base_price + 0.10
                l = base_price - 0.10
            else:
                o = base_price + random.uniform(-1.0, 1.0)
                c = o + random.uniform(-0.5, 0.5)
                h = max(o, c) + random.uniform(0.1, 0.5)
                l = min(o, c) - random.uniform(0.1, 0.5)

            payloads.append({
                "symbol": symbol_id,
                "interval": timeframe,
                "kline": {
                    "t": t, "o": o, "h": h, "l": l, "c": c,
                    "v": 1000.0, "closed": True,
                },
            })

        log.info(f"Synthetic kline router: generated {len(payloads)} candles for {symbol_id}")
        return payloads


# Singleton instance
kline_router = KlineRouter()