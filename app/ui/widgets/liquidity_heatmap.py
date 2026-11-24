"""Liquidity Heatmap — Subclass of CandlestickCanvas.

Inherits all the robust rendering, zooming, and panning from CandlestickCanvas.
Adds a live L2 liquidity heatmap drawn behind the candles.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.core.config import config
from app.core.event_bus import bus
from app.core.symbols import registry
from app.ui.dialogs.candlestick import CandlestickCanvas

log = logging.getLogger(__name__)


class LiquidityHeatmap(CandlestickCanvas):
    widget_id = "liquidity_heatmap"

    def __init__(self, symbol_id: str = "", interval: str = "1m", target_days: int = 1) -> None:
        # Initialize the parent canvas
        super().__init__(symbol_id=symbol_id, interval=interval, target_days=target_days)
        
        self._l2_history: deque = deque()
        self._last_l2_update = 0
        self._bucket_size = 0.0
        self._bucket_mid = 0.0

        # Subscribe to L2 book updates
        bus.subscribe(config.protocol.topics["book_update"], self._on_l2_book)

    def cleanup(self) -> None:
        super().cleanup()
        try:
            bus.unsubscribe(config.protocol.topics["book_update"], self._on_l2_book)
        except Exception:
            pass

    async def _on_l2_book(self, _topic: str, payload: dict) -> None:
        if not self._alive or payload.get("symbol") != self._symbol_id:
            return
        
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_l2_update < 100: # 10Hz sampling
            return
        self._last_l2_update = now_ms

        book = payload.get("data") or payload.get("book") or payload
        raw_bids = book.get("bids", [])
        raw_asks = book.get("asks", [])
        if not raw_bids or not raw_asks: return

        try:
            bids = {float(p): float(q) for p, q in raw_bids[:1000] if float(q) > 0}
            asks = {float(p): float(q) for p, q in raw_asks[:1000] if float(q) > 0}
        except (TypeError, ValueError): return

        if not bids or not asks: return
        mid = (max(bids.keys()) + min(asks.keys())) / 2.0
        if mid <= 0: return

        if (self._bucket_size <= 0 or self._bucket_mid <= 0
                or abs(mid - self._bucket_mid) / self._bucket_mid > 0.10):
            self._bucket_size = mid * 0.0001 
            self._bucket_mid = mid
            self._l2_history.clear()

        self._l2_history.append({
            "ts": now_ms,
            "bids": {round(p/self._bucket_size)*self._bucket_size: q for p,q in bids.items()},
            "asks": {round(p/self._bucket_size)*self._bucket_size: q for p,q in asks.items()},
        })

        # Keep only last 2 hours of L2 history
        cutoff = now_ms - (2 * 60 * 60 * 1000)
        while self._l2_history and self._l2_history[0]["ts"] < cutoff:
            self._l2_history.popleft()

        # Trigger a repaint
        if self._alive:
            self.update()

    def paintEvent(self, event) -> None:
        # We intercept the paintEvent to draw the heatmap behind the candles,
        # then let the parent class draw the candles, grids, and axes on top.
        
        # 1. Draw the heatmap if we have candles and L2 data
        if self._candles and self._l2_history:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            
            W = self.width()
            H = self.height()
            theme = config.ui.theme
            
            # These match the parent's internal layout parameters
            self.pad_top = 40
            self.pad_bottom = 35
            self.pad_left = 65
            self.pad_right = 95
            
            plot_w = max(W - self.pad_left - self.pad_right, 1)
            plot_h = max(H - self.pad_top - self.pad_bottom, 1)
            right_edge_x = self.pad_left + plot_w

            n = len(self._candles)
            
            # Match the parent's x_of logic
            def x_of(index: float) -> float:
                return right_edge_x - self.right_offset_px - (n - 1 - index) * self.bar_width_px

            # Helper to convert timestamp to continuous x coordinate
            def x_of_time(ts: int) -> float:
                times = [c["t"] for c in self._candles]
                idx = bisect.bisect_left(times, ts)
                if idx == 0: return x_of(0)
                if idx >= len(times): return x_of(len(times) - 1)
                
                c1 = self._candles[idx-1]
                c2 = self._candles[idx]
                frac = (ts - c1["t"]) / max(1, c2["t"] - c1["t"])
                return x_of(idx-1) + frac * (x_of(idx) - x_of(idx-1))

            # Compute visible slice
            start_idx = max(0, int((self.pad_left - (right_edge_x - self.right_offset_px)) / self.bar_width_px + (n - 1)) - 2)
            end_idx = min(n, int((right_edge_x - (right_edge_x - self.right_offset_px)) / self.bar_width_px + (n - 1)) + 3)
            
            if start_idx < end_idx:
                visible_candles = self._candles[start_idx:end_idx]
                t_start = visible_candles[0]["t"]
                t_end = visible_candles[-1]["t"]
                
                # Y-axis bounds from visible candles
                c_low = min(c["l"] for c in visible_candles)
                c_high = max(c["h"] for c in visible_candles)
                span = (c_high - c_low) * 1.1 if c_high > c_low else max(abs(c_high) * 0.001, 1.0)
                center = (c_high + c_low) / 2.0
                
                p_min = center - span / 2.0
                p_max = center + span / 2.0
                if p_max <= p_min: p_max = p_min + 1.0

                def y_of(price: float) -> float:
                    return self.pad_top + (p_max - price) / (p_max - p_min) * plot_h

                # Filter L2 history
                visible_l2 = [f for f in self._l2_history if t_start <= f["ts"] <= t_end + 60000]
                
                if visible_l2:
                    max_qty = 1e-6
                    for f in visible_l2:
                        for q in f["bids"].values(): max_qty = max(max_qty, q)
                        for q in f["asks"].values(): max_qty = max(max_qty, q)

                    min_qty = max_qty * 0.05 # 5% threshold
                    row_h = max(2.0, plot_h / 100.0)
                    
                    prev_col = -1
                    
                    for f in visible_l2:
                        x = x_of_time(f["ts"])
                        if prev_col != -1 and x > prev_col:
                            w_rect = x - prev_col
                            
                            # Bids (Neon Cyan)
                            for bp, qty in f["bids"].items():
                                if qty < min_qty: continue
                                if bp < p_min or bp > p_max: continue
                                y = y_of(bp)
                                inten = min(1.0, (qty / max_qty) ** 0.35)
                                color = QColor(0, int(200 + 55 * inten), 255)
                                color.setAlpha(180)
                                p.fillRect(QRectF(prev_col, y - row_h/2, w_rect, row_h), color)

                            # Asks (Neon Red)
                            for bp, qty in f["asks"].items():
                                if qty < min_qty: continue
                                if bp < p_min or bp > p_max: continue
                                y = y_of(bp)
                                inten = min(1.0, (qty / max_qty) ** 0.35)
                                color = QColor(255, int(50 + 100 * (1 - inten)), int(50 + 100 * (1 - inten)))
                                color.setAlpha(180)
                                p.fillRect(QRectF(prev_col, y - row_h/2, w_rect, row_h), color)
                                
                        prev_col = x

            p.end()

        # 2. Let the parent draw the candles, axes, and zones on top
        super().paintEvent(event)