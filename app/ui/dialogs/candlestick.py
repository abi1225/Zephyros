from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Any

import aiohttp
from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QWheelEvent, QMouseEvent
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from app.core.config import config
from app.core.event_bus import bus
from app.core.symbols import registry

log = logging.getLogger(__name__)


def _candle_cfg() -> dict[str, Any]:
    top = getattr(config.ui, "candlestick", None)
    if top:
        return dict(top)
    return {}


_CLASS_STYLE = {
    "structural": {"letter": "S", "border": Qt.SolidLine, "thickness": 2.0,
                   "fill_mult": 1.00, "edge_mult": 1.00},
    "strong":     {"letter": "M", "border": Qt.SolidLine, "thickness": 1.5,
                   "fill_mult": 0.85, "edge_mult": 0.90},
    "major":      {"letter": "M", "border": Qt.SolidLine, "thickness": 1.5,
                   "fill_mult": 0.85, "edge_mult": 0.90},
    "standard":   {"letter": "m", "border": Qt.DashLine,  "thickness": 1.0,
                   "fill_mult": 0.70, "edge_mult": 0.80},
    "moderate":   {"letter": "m", "border": Qt.DashLine,  "thickness": 1.0,
                   "fill_mult": 0.70, "edge_mult": 0.80},
    "minor":      {"letter": "i", "border": Qt.DashLine,  "thickness": 1.0,
                   "fill_mult": 0.50, "edge_mult": 0.65},
    "intraday":   {"letter": "d", "border": Qt.DotLine,   "thickness": 1.0,
                   "fill_mult": 0.35, "edge_mult": 0.50},
}


def _class_style(cls: str) -> dict:
    return _CLASS_STYLE.get(cls, _CLASS_STYLE["intraday"])


class CandlestickCanvas(QWidget):
    """TradingView-inspired professional candlestick chart canvas."""

    view_changed = Signal(int, int)

    def __init__(
        self,
        symbol_id: str = "",
        interval: str = "5m",
        target_days: int = 7,
    ) -> None:
        super().__init__()
        self.setMinimumSize(600, 400)
        self.setStyleSheet("background: transparent;")
        self.setFocusPolicy(Qt.StrongFocus)

        self._symbol_id = symbol_id
        self._interval = interval
        self._target_days = target_days
        self._limit = 1000  # Calculated dynamically in _fetch_initial

        self._candles: list[dict] = []
        self._zones_demand: list[dict] = []
        self._zones_supply: list[dict] = []
        self._voids_demand: list[dict] = []
        self._voids_supply: list[dict] = []
        self._daily_profiles: list[dict] = []

        self._loading = False
        self._error: str = ""

        self._alive = True
        self.destroyed.connect(self._on_destroyed)

        cfg = _candle_cfg()
        self.candle_frac: float = float(cfg.get("candle_width_frac", 0.75))
        self.wick_sanity_pct: float = float(cfg.get("wick_sanity_pct", 0.02))

        self.pad_top: int = 40
        self.pad_bottom: int = 35
        self.pad_left: int = 65
        self.pad_right: int = 95

        self.price_ticks: int = int(cfg.get("price_axis_ticks", 8))
        self.time_ticks: int = int(cfg.get("time_axis_ticks", 8))
        self.zone_fill_alpha: float = float(cfg.get("zone_fill_alpha", 0.32))
        self.zone_edge_alpha: float = float(cfg.get("zone_edge_alpha", 0.90))
        self.zone_invalid_alpha: float = float(cfg.get("zone_invalid_alpha", 0.10))
        self.min_band_height_px: int = int(cfg.get("min_band_height_px", 6))

        self.bar_width_px: float = 10.0
        self.min_bar_width: float = 2.0
        self.max_bar_width: float = 60.0
        self.right_offset_px: float = 60.0

        self._manual_price_center: float | None = None
        self._manual_price_span: float | None = None

        self._dragging: bool = False
        self._drag_start_pos: QPoint = QPoint()
        self._drag_start_right_offset: float = 0.0
        self._drag_start_price_center: float = 0.0
        self._drag_mode: str = "all"

        self._topic_kline = config.protocol.topics["feed_kline"]
        self._topic_zones = config.protocol.topics["zones_update"]
        self._topic_profile = config.protocol.topics.get("volume_profile", "volume_profile")

        bus.subscribe(self._topic_kline, self._on_kline)
        bus.subscribe(self._topic_zones, self._on_zones)
        bus.subscribe(self._topic_profile, self._on_volume_profile)

        self._fetch_task: asyncio.Task | None = None
        self._schedule_fetch()

    def _on_destroyed(self, *_args) -> None:
        self._alive = False

    def cleanup(self) -> None:
        self._alive = False
        for topic, handler in (
            (self._topic_kline, self._on_kline),
            (self._topic_zones, self._on_zones),
            (self._topic_profile, self._on_volume_profile),
        ):
            try:
                bus.unsubscribe(topic, handler)
            except Exception:
                pass
        if self._fetch_task and not self._fetch_task.done():
            self._fetch_task.cancel()

    def reload(self, symbol_id: str, interval: str, target_days: int = 7) -> None:
        self._symbol_id = symbol_id
        self._interval = interval
        self._target_days = target_days
        self._candles = []
        self._error = ""
        self.bar_width_px = 10.0
        self.right_offset_px = 60.0
        self._manual_price_center = None
        self._manual_price_span = None
        self._daily_profiles = []
        self.update()
        self._schedule_fetch()

    def _reset_view(self) -> None:
        self.bar_width_px = 10.0
        self.right_offset_px = 60.0
        self._manual_price_center = None
        self._manual_price_span = None
        self.update()

    def _get_interval_minutes(self, interval: str) -> int:
        try:
            unit = interval[-1].lower()
            value = int(interval[:-1])
            mult = {"m": 1, "h": 60, "d": 1440, "w": 10080}.get(unit)
            return value * mult if mult else 5
        except Exception:
            return 5

    # ─── Data Fetching ──────────────────────────────────────

    def _schedule_fetch(self) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if self._fetch_task and not self._fetch_task.done():
            self._fetch_task.cancel()
        self._fetch_task = loop.create_task(self._fetch_initial())

    async def _fetch_initial(self) -> None:
        if not self._symbol_id:
            self._error = "no symbol selected"
            if self._alive:
                self.update()
            return

        sym = registry.get(self._symbol_id)
        if sym is None or not sym.binance:
            self._error = f"{self._symbol_id} has no Binance mapping"
            if self._alive:
                self.update()
            return

        self._loading = True
        if self._alive:
            self.update()

        interval_mins = self._get_interval_minutes(self._interval)
        total_minutes = self._target_days * 24 * 60
        self._limit = max(100, total_minutes // interval_mins)

        rest = config.feeds.binance.rest_base.rstrip("/")
        all_data = []
        end_time = None
        remaining = self._limit

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while remaining > 0:
                    req_limit = min(remaining, 1000)
                    url = f"{rest}/api/v3/klines?symbol={sym.binance}&interval={self._interval}&limit={req_limit}"
                    if end_time:
                        url += f"&endTime={end_time}"

                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        batch = await resp.json()

                    if not batch:
                        break

                    all_data = batch + all_data
                    remaining -= len(batch)
                    end_time = int(batch[0][0]) - 1

                    # Small sleep to prevent hammering the REST API
                    await asyncio.sleep(0.1)

        except Exception as exc:
            log.warning("Candlestick fetch failed: %s", exc)
            self._loading = False
            self._error = f"fetch failed: {exc}"
            if self._alive:
                self.update()
            return

        if not self._alive:
            return

        log.info("Fetched %d candles from Binance (requested %d for %d days)", len(all_data), self._limit, self._target_days)

        fetched = []
        for row in all_data:
            try:
                o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])

                if self.wick_sanity_pct > 0:
                    if h > max(o, c) * (1.0 + self.wick_sanity_pct):
                        h = max(o, c)
                    if l < min(o, c) * (1.0 - self.wick_sanity_pct):
                        l = min(o, c)

                fetched.append({
                    "t": int(row[0]), "o": o, "h": h, "l": l,
                    "c": c, "v": float(row[5]),
                })
            except Exception:
                continue

        by_t = {c["t"]: c for c in fetched}
        for c in self._candles:
            by_t[int(c["t"])] = c
        self._candles = sorted(by_t.values(), key=lambda c: c["t"])

        if len(self._candles) > self._limit:
            self._candles = self._candles[-self._limit:]

        self._loading = False
        self._error = ""
        self.update()

    async def _on_kline(self, _topic: str, payload: dict) -> None:
        if not self._alive or payload.get("symbol") != self._symbol_id:
            return
        if payload.get("interval") and payload.get("interval") != self._interval:
            return

        k = payload.get("kline")
        if not k:
            return

        t = int(k["t"])
        o, h, l, c, v = float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])

        if self.wick_sanity_pct > 0:
            if h > max(o, c) * (1.0 + self.wick_sanity_pct):
                h = max(o, c)
            if l < min(o, c) * (1.0 - self.wick_sanity_pct):
                l = min(o, c)

        candle = {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}

        replaced = False
        for i, existing in enumerate(self._candles):
            if existing["t"] == t:
                self._candles[i] = candle
                replaced = True
                break
        if not replaced:
            self._candles.append(candle)

        self._candles.sort(key=lambda x: x["t"])
        if len(self._candles) > self._limit:
            self._candles = self._candles[-self._limit:]

        if self._alive:
            self.update()

    async def _on_zones(self, _topic: str, payload: dict) -> None:
        if not self._alive or (
            payload.get("symbol") and payload.get("symbol") != self._symbol_id
        ):
            return

        self._zones_demand = list(payload.get("demand") or [])
        self._zones_supply = list(payload.get("supply") or [])

        # Match the keys the builder / strategy actually emit
        self._voids_demand = list(payload.get("voids_demand") or [])
        self._voids_supply = list(payload.get("voids_supply") or [])

        if self._alive:
            self.update()

    async def _on_volume_profile(self, _topic: str, payload: dict) -> None:
        if not self._alive or (payload.get("symbol") and payload.get("symbol") != self._symbol_id):
            return
        self._daily_profiles = list(payload.get("daily_profiles") or [])
        if self._alive:
            self.update()

    # ─── Mouse & Keyboard Interactions ──────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._candles:
            pos = event.position().toPoint()
            self._dragging = True
            self._drag_start_pos = pos
            self._drag_start_right_offset = self.right_offset_px
            if self._manual_price_center is not None:
                self._drag_start_price_center = self._manual_price_center

            if pos.y() > self.height() - self.pad_bottom:
                self._drag_mode = "time_axis"
                self.setCursor(Qt.SizeHorCursor)
            elif pos.x() > self.width() - self.pad_right:
                self._drag_mode = "price_axis"
                self.setCursor(Qt.SizeVerCursor)
            else:
                self._drag_mode = "all"
                self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging or not self._candles:
            return

        pos = event.position().toPoint()
        dx = pos.x() - self._drag_start_pos.x()
        dy = pos.y() - self._drag_start_pos.y()

        plot_h = max(self.height() - self.pad_top - self.pad_bottom, 1)

        if self._drag_mode in ("all", "time_axis"):
            self.right_offset_px = self._drag_start_right_offset - dx

            n = len(self._candles)
            min_off = self.pad_left - (n - 1) * self.bar_width_px
            max_off = self.width() - self.pad_right
            self.right_offset_px = max(min_off, min(max_off, self.right_offset_px))

        if self._drag_mode in ("all", "price_axis") and self._manual_price_span is not None:
            price_per_px = self._manual_price_span / plot_h
            self._manual_price_center = self._drag_start_price_center + dy * price_per_px

        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self.unsetCursor()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._reset_view()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._candles:
            return

        pos = event.position().toPoint()
        W = self.width()
        H = self.height()

        if pos.x() < self.pad_left or pos.x() > W - self.pad_right:
            if pos.x() > W - self.pad_right:
                angle = event.angleDelta().y()
                if angle != 0 and self._manual_price_span is not None:
                    factor = 0.9 if angle > 0 else 1.1
                    self._manual_price_span *= factor
                    self.update()
            return

        if pos.y() < self.pad_top or pos.y() > H - self.pad_bottom:
            return

        angle = event.angleDelta().y()
        if angle == 0:
            return

        n = len(self._candles)
        right_edge_x = W - self.pad_right
        old_bar_w = self.bar_width_px

        factor = 1.15 if angle > 0 else 1.0 / 1.15
        new_bar_w = max(self.min_bar_width, min(self.max_bar_width, old_bar_w * factor))

        if new_bar_w != old_bar_w:
            mouse_x = pos.x()
            i_mouse = (mouse_x - (right_edge_x - self.right_offset_px)) / old_bar_w + (n - 1)

            self.bar_width_px = new_bar_w
            self.right_offset_px = right_edge_x - mouse_x - (n - 1 - i_mouse) * new_bar_w

            min_off = self.pad_left - (n - 1) * self.bar_width_px
            max_off = self.width() - self.pad_right
            self.right_offset_px = max(min_off, min(max_off, self.right_offset_px))

            self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Home:
            self._reset_view()
        elif event.key() == Qt.Key_Left:
            self.right_offset_px += 20.0
            n = len(self._candles)
            min_off = self.pad_left - (n - 1) * self.bar_width_px
            self.right_offset_px = max(min_off, self.right_offset_px)
            self.update()
        elif event.key() == Qt.Key_Right:
            self.right_offset_px -= 20.0
            max_off = self.width() - self.pad_right
            self.right_offset_px = min(max_off, self.right_offset_px)
            self.update()
        else:
            super().keyPressEvent(event)

    # ─── Rendering / Painting ───────────────────────────────

    def paintEvent(self, _event) -> None:
        if not self._candles:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            theme = config.ui.theme
            p.fillRect(self.rect(), QColor(theme.surfaces.l0))
            p.setPen(QPen(QColor(theme.text.muted)))
            p.setFont(QFont(theme.fonts.ui, theme.fonts.size_md))
            msg = "loading candles…" if self._loading else (self._error or "no data")
            p.drawText(self.rect(), Qt.AlignCenter, msg)
            return

        W = self.width()
        H = self.height()
        theme = config.ui.theme
        n = len(self._candles)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), QColor(theme.surfaces.l0))

        plot_w = max(W - self.pad_left - self.pad_right, 1)
        plot_h = max(H - self.pad_top - self.pad_bottom, 1)
        right_edge_x = self.pad_left + plot_w

        def x_of(index: float) -> float:
            return right_edge_x - self.right_offset_px - (n - 1 - index) * self.bar_width_px

        i_left = (self.pad_left - (right_edge_x - self.right_offset_px)) / self.bar_width_px + (n - 1)
        i_right = (right_edge_x - (right_edge_x - self.right_offset_px)) / self.bar_width_px + (n - 1)

        start_idx = max(0, int(i_left) - 2)
        end_idx = min(n, int(i_right) + 3)
        visible_subset = self._candles[start_idx:end_idx] or self._candles

        c_low = min(c["l"] for c in visible_subset)
        c_high = max(c["h"] for c in visible_subset)
        span = (c_high - c_low) * 1.1 if c_high > c_low else max(abs(c_high) * 0.001, 1.0)
        center = (c_high + c_low) / 2.0

        if self._manual_price_center is None or self._manual_price_span is None:
            self._manual_price_center = center
            self._manual_price_span = span

        p_min = self._manual_price_center - self._manual_price_span / 2.0
        p_max = self._manual_price_center + self._manual_price_span / 2.0
        if p_max <= p_min:
            p_max = p_min + 1.0

        def y_of(price: float) -> float:
            return self.pad_top + (p_max - price) / (p_max - p_min) * plot_h

        grid_pen = QPen(QColor(theme.borders.subtle), 1, Qt.DotLine)
        p.setPen(grid_pen)
        for i in range(1, self.price_ticks):
            price = p_min + (p_max - p_min) * i / self.price_ticks
            y = y_of(price)
            p.drawLine(self.pad_left, int(y), right_edge_x, int(y))

        current_price = self._candles[-1]["c"]

        p.save()
        p.setClipRect(self.pad_left, self.pad_top, plot_w, plot_h)

        now_utc = _dt.datetime.now(_dt.timezone.utc)
        start_of_day_ms = int(now_utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

        for i, c in enumerate(self._candles):
            if c["t"] >= start_of_day_ms:
                sx = x_of(i)
                if sx < right_edge_x:
                    session_bg = QColor(theme.accent.main)
                    session_bg.setAlphaF(0.025)
                    p.fillRect(QRectF(sx, self.pad_top, right_edge_x - sx, plot_h), session_bg)
                break

        # ──────────────────────────────────────────────────
        # PAINT ORDER (back → front):
        #   1. Daily profile histograms (background layer)
        #   2. Voids (faint dashed bands over profiles)
        #   3. Zones (bright blocks over everything)
        #   4. Candles
        # ──────────────────────────────────────────────────

        # ─── 1. Daily profile histograms (BACKGROUND) ───
        if self._daily_profiles:
            for dp in self._daily_profiles:
                day_start_ms = dp.get("start_ms", 0)
                day_end_ms = dp.get("end_ms", 0)
                rows = dp.get("rows", [])
                poc = float(dp.get("poc", 0))
                max_val = float(dp.get("max_val", 0))
                profile_alpha = float(dp.get("alpha", 0.55))
                day_label = str(dp.get("label", ""))

                if not rows or day_start_ms <= 0 or day_end_ms <= 0 or max_val <= 0:
                    continue

                start_i = -1
                for i, c in enumerate(self._candles):
                    if c["t"] >= day_start_ms:
                        start_i = i
                        break

                end_i = -1
                for i in range(len(self._candles) - 1, -1, -1):
                    if self._candles[i]["t"] <= day_end_ms:
                        end_i = i
                        break

                if start_i == -1 or end_i == -1 or start_i > end_i:
                    continue

                day_x_start = x_of(start_i)
                day_x_end = x_of(end_i)

                if day_x_end < self.pad_left or day_x_start > right_edge_x:
                    continue

                day_width_px = max(day_x_end - day_x_start, 1.0)

                min_r = min(float(r["low"]) for r in rows)
                max_r = max(float(r["high"]) for r in rows)

                y_bg_top = max(y_of(max_r), float(self.pad_top))
                y_bg_bot = min(y_of(min_r), float(self.pad_top + plot_h))

                day_bg = QColor(theme.surfaces.l3)
                day_bg.setAlphaF(min(0.25, profile_alpha * 0.25))
                p.setPen(Qt.NoPen)
                p.setBrush(day_bg)
                if y_bg_bot > y_bg_top:
                    p.drawRect(QRectF(day_x_start, y_bg_top, day_width_px, y_bg_bot - y_bg_top))

                rail = QColor(theme.text.muted)
                rail.setAlphaF(min(1.0, profile_alpha * 1.5))
                p.setPen(QPen(rail, 1))
                p.drawLine(int(day_x_start), int(y_of(max_r)), int(day_x_start + day_width_px), int(y_of(max_r)))
                p.drawLine(int(day_x_start), int(y_of(min_r)), int(day_x_start + day_width_px), int(y_of(min_r)))

                base_x = day_x_start
                max_profile_w = day_width_px * 0.30

                up_color = QColor("#26a69a")
                down_color = QColor("#ef5350")

                for r in rows:
                    try:
                        r_low, r_high = float(r["low"]), float(r["high"])
                        up_v, down_v = float(r["up_vol"]), float(r["down_vol"])
                        total_v = up_v + down_v
                    except Exception:
                        continue

                    if total_v <= 0:
                        continue

                    y_l, y_h = y_of(r_low), y_of(r_high)
                    y_top_c = max(min(y_l, y_h), float(self.pad_top))
                    y_bot_c = min(max(y_l, y_h), float(self.pad_top + plot_h))
                    h = y_bot_c - y_top_c

                    if h <= 0:
                        continue

                    h_draw = max(1.0, h)

                    ratio = total_v / max_val
                    w = max(1.0, ratio * max_profile_w)

                    w_up = w * (up_v / total_v)
                    w_down = w * (down_v / total_v)

                    p.setPen(Qt.NoPen)

                    if w_up > 0 and w_down > 0:
                        up_color.setAlphaF(profile_alpha)
                        p.setBrush(up_color)
                        p.drawRect(QRectF(base_x, y_top_c, w_up, h_draw))

                        down_color.setAlphaF(profile_alpha)
                        p.setBrush(down_color)
                        p.drawRect(QRectF(base_x + w_up, y_top_c, w_down, h_draw))
                    elif w_up > 0:
                        up_color.setAlphaF(profile_alpha)
                        p.setBrush(up_color)
                        p.drawRect(QRectF(base_x, y_top_c, w, h_draw))
                    elif w_down > 0:
                        down_color.setAlphaF(profile_alpha)
                        p.setBrush(down_color)
                        p.drawRect(QRectF(base_x, y_top_c, w, h_draw))

                    divider_color = QColor(theme.surfaces.l0)
                    p.setPen(QPen(divider_color, 1))
                    p.drawLine(int(base_x), int(y_bot_c - 1), int(base_x + w), int(y_bot_c - 1))

                if poc > 0:
                    poc_color = QColor("#ffffff")
                    poc_color.setAlphaF(min(1.0, profile_alpha * 1.4))
                    p.setPen(QPen(poc_color, 1.0, Qt.SolidLine))
                    y_poc = y_of(poc)
                    p.drawLine(int(day_x_start), int(y_poc), int(day_x_start + day_width_px), int(y_poc))

                if day_label:
                    label_color = QColor(theme.text.muted)
                    label_color.setAlphaF(min(0.95, profile_alpha * 1.5))
                    p.setPen(QPen(label_color))
                    p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))
                    label_y = max(int(y_of(max_r) - 4), self.pad_top + 12)
                    p.drawText(int(day_x_start + 4), int(label_y), day_label)

        # ─── 2. Live Voids (over profiles) ───
        for voids, side in ((self._voids_demand, "demand"), (self._voids_supply, "supply")):
            for v in voids:
                try:
                    v_low, v_high = float(v["low"]), float(v["high"])
                except Exception:
                    continue
                if v_high < p_min or v_low > p_max:
                    continue
                self._draw_void(p, theme, v, side, y_of, plot_w, plot_h)

        # ─── 3. SMC Zones (over everything) ───
        for zones, side in ((self._zones_demand, "demand"), (self._zones_supply, "supply")):
            for z in zones:
                try:
                    z_low, z_high, z_peak = float(z["low"]), float(z["high"]), float(z["peak"])
                except Exception:
                    continue
                if z_high < p_min or z_low > p_max:
                    continue
                self._draw_zone(p, theme, z, side, y_of, plot_w, plot_h)

        # ─── 4. Candlesticks ───
        body_w = max(1.5, self.bar_width_px * min(self.candle_frac, 0.85))
        bull = QColor(theme.semantic.up)
        bear = QColor(theme.semantic.down)

        for i in range(start_idx, end_idx):
            c = self._candles[i]
            cx = x_of(i) + self.bar_width_px / 2.0
            if cx < self.pad_left - body_w or cx > right_edge_x + body_w:
                continue

            o, h, l, cl = c["o"], c["h"], c["l"], c["c"]
            up = cl >= o
            color = bull if up else bear

            p.setPen(QPen(color, 1))
            p.drawLine(int(cx), int(y_of(h)), int(cx), int(y_of(l)))

            y_o, y_c = y_of(o), y_of(cl)
            top = min(y_o, y_c)
            h_rect = max(1.0, abs(y_c - y_o))
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRect(QRectF(cx - body_w / 2.0, top, body_w, h_rect))

        p.restore()

        y_last = y_of(current_price)
        last_line_col = QColor(theme.text.primary)
        last_line_col.setAlpha(140)
        p.setPen(QPen(last_line_col, 1, Qt.DashLine))
        p.drawLine(self.pad_left, int(y_last), right_edge_x, int(y_last))

        p.setPen(QPen(QColor(theme.text.muted)))
        p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))
        for i in range(self.price_ticks + 1):
            price = p_min + (p_max - p_min) * i / self.price_ticks
            y = y_of(price)
            p.drawText(right_edge_x + 6, int(y + 4), f"{price:.2f}")

        p.setPen(QPen(QColor(theme.text.primary)))
        p.setFont(QFont(theme.fonts.mono, theme.fonts.size_md))
        p.drawText(right_edge_x + 6, int(y_last + 4), f"{current_price:.2f}")

        p.setPen(QPen(QColor(theme.text.muted)))
        p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))
        for i in range(self.time_ticks + 1):
            x = self.pad_left + plot_w * i / self.time_ticks
            i_idx = (x - (right_edge_x - self.right_offset_px)) / self.bar_width_px + (n - 1)
            idx_int = max(0, min(n - 1, int(round(i_idx))))
            t_ms = self._candles[idx_int]["t"]
            label = _dt.datetime.fromtimestamp(t_ms / 1000, _dt.timezone.utc).strftime("%H:%M")
            p.drawText(int(x - 20), H - 10, label)

        p.setPen(QPen(QColor(theme.text.muted)))
        p.setFont(QFont(theme.fonts.ui, theme.fonts.size_sm))
        summary = f"{self._symbol_id}  ·  {self._interval}  ·  zoom {self.bar_width_px:.1f}px/bar"
        if self._daily_profiles:
            labels = [str(dp.get("label", "?")) for dp in self._daily_profiles]
            summary += f"  ·  profiles: {' '.join(labels)}"
        p.drawText(self.pad_left, self.pad_top - 12, summary)

    # ─── Drawing Helpers ────────────────────────────

    def _draw_void(self, p, theme, v, side, y_of, plot_w, plot_h) -> None:
        v_low, v_high = float(v["low"]), float(v["high"])
        v_peak = float(v["peak"])
        quality = float(v.get("quality", 0.0))
        void_class = str(v.get("class", v.get("tier", "intraday")))
        confluence = int(v.get("confluence", 1))
        void_type = str(v.get("void_type", "UNCLASSIFIED"))
        threshold = float(v.get("threshold", 55.0))

        style = _class_style(void_class)

        y_high, y_low = y_of(v_high), y_of(v_low)
        y_top_c = max(min(y_high, y_low), float(self.pad_top))
        y_bot_c = min(max(y_high, y_low), float(self.pad_top + plot_h))

        band_h = y_bot_c - y_top_c
        if band_h < self.min_band_height_px:
            center = (y_top_c + y_bot_c) / 2.0
            half = self.min_band_height_px / 2.0
            y_top_c = center - half
            y_bot_c = center + half
            if y_top_c < self.pad_top:
                shift = self.pad_top - y_top_c
                y_top_c += shift
                y_bot_c += shift
            if y_bot_c > self.pad_top + plot_h:
                shift = y_bot_c - (self.pad_top + plot_h)
                y_top_c -= shift
                y_bot_c -= shift

        rect_h = max(1.0, y_bot_c - y_top_c)

        fill = QColor(theme.text.muted)
        fill.setAlphaF(0.02 + 0.03 * style["fill_mult"])
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawRect(QRectF(self.pad_left, y_top_c, plot_w, rect_h))

        edge = QColor(theme.text.muted)
        edge.setAlphaF(0.5 * style["edge_mult"])
        p.setPen(QPen(edge, style["thickness"], style["border"]))
        p.drawLine(self.pad_left, int(y_top_c), self.pad_left + plot_w, int(y_top_c))
        p.drawLine(self.pad_left, int(y_bot_c), self.pad_left + plot_w, int(y_bot_c))

        badge = f"[{style['letter']}{confluence}]"
        type_tag = {"DISPLACEMENT": "DIS", "EXHAUSTION": "EXH"}.get(
            void_type, "UNK",
        )
        label = (
            f"{badge} VOID  {v_peak:.2f}  "
            f"w={v_high - v_low:.2f}  "
            f"Q={quality:.0f}/{threshold:.0f}  "
            f"{type_tag}"
        )

        label_color = QColor(theme.text.muted)
        label_color.setAlphaF(0.15 + 0.7 * style["edge_mult"])
        p.setPen(QPen(label_color))
        p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))

        if rect_h >= 18:
            p.drawText(
                QRectF(self.pad_left + 6, y_top_c, plot_w - 12, rect_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
        else:
            label_y = max(y_top_c - 3, self.pad_top + 12)
            p.drawText(int(self.pad_left + 6), int(label_y), label)

    def _draw_zone(self, p, theme, z, side, y_of, plot_w, plot_h) -> None:
        z_low, z_high, z_peak = float(z["low"]), float(z["high"]), float(z["peak"])
        valid = bool(z.get("valid", True))
        origin = z.get("origin", "live")
        bbi = float(z.get("bbi", 0.0))
        battle_state = str(z.get("battle_state", "WATCH"))
        quality = float(z.get("quality", 0.0))
        zone_class = str(z.get("class", z.get("tier", "intraday")))
        confluence = int(z.get("confluence", 1))
        penalty = float(z.get("quality_penalty", 0.0))
        threshold = float(z.get("threshold", 55.0))
        validated = bool(z.get("validated", True))

        style = _class_style(zone_class)

        base = QColor(theme.semantic.up if side == "demand" else theme.semantic.down)

        if battle_state == "ARMED":
            edge_color = QColor("#ffcc00")
        elif battle_state == "BREAK_BUILDING":
            edge_color = QColor(theme.semantic.down)
        elif battle_state == "HOLD_BUILDING":
            edge_color = QColor(theme.semantic.up)
        elif battle_state == "STALEMATE":
            edge_color = QColor(theme.text.secondary)
        else:
            edge_color = QColor(base)

        if not valid:
            fill_alpha = self.zone_invalid_alpha
            edge_alpha = self.zone_invalid_alpha
        else:
            base_fill = self.zone_fill_alpha * style["fill_mult"]
            if origin in ("historical", "profile"):
                base_fill = max(0.22, base_fill)
            fill_alpha = base_fill

            edge_alpha = self.zone_edge_alpha * style["edge_mult"]
            if origin in ("historical", "profile"):
                edge_alpha *= 0.85

            # Dim zones that haven't been order-block-validated.
            # Currently defaults to True so nothing changes visually
            # until validation is wired up in the builder.
            if not validated:
                fill_alpha *= 0.4
                edge_alpha *= 0.5

        fill = QColor(base)
        fill.setAlphaF(max(0.0, min(1.0, fill_alpha)))
        edge_color.setAlphaF(max(0.0, min(1.0, edge_alpha)))

        y_high, y_low = y_of(z_high), y_of(z_low)
        y_top_c = max(min(y_high, y_low), float(self.pad_top))
        y_bot_c = min(max(y_high, y_low), float(self.pad_top + plot_h))

        band_h = y_bot_c - y_top_c
        if band_h < self.min_band_height_px:
            center = (y_top_c + y_bot_c) / 2.0
            half = self.min_band_height_px / 2.0
            y_top_c = center - half
            y_bot_c = center + half
            if y_top_c < self.pad_top:
                shift = self.pad_top - y_top_c
                y_top_c += shift
                y_bot_c += shift
            if y_bot_c > self.pad_top + plot_h:
                shift = y_bot_c - (self.pad_top + plot_h)
                y_top_c -= shift
                y_bot_c -= shift

        rect_h = max(1.0, y_bot_c - y_top_c)

        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawRect(QRectF(self.pad_left, y_top_c, plot_w, rect_h))

        p.setPen(QPen(edge_color, style["thickness"], style["border"]))
        p.drawLine(self.pad_left, int(y_top_c), self.pad_left + plot_w, int(y_top_c))
        p.drawLine(self.pad_left, int(y_bot_c), self.pad_left + plot_w, int(y_bot_c))

        badge = f"[{style['letter']}{confluence}]"
        tag = "DEMAND" if side == "demand" else "SUPPLY"
        state_tag = "" if valid else " [forming]"
        hist_tag = " [H]" if origin in ("historical", "profile") else ""
        val_tag = "" if validated else " (u)"

        glyph = ""
        if battle_state == "ARMED":
            glyph = "⚡ "
        elif battle_state == "BREAK_BUILDING":
            glyph = "▼ "
        elif battle_state == "HOLD_BUILDING":
            glyph = "▲ "
        elif battle_state == "STALEMATE":
            glyph = "◇ "

        penalty_tag = f" ⚠{penalty:.0f}" if penalty > 0 else ""

        label = (
            f"{badge} {glyph}{tag}{state_tag}{hist_tag}{val_tag}  "
            f"{z_peak:.2f}  "
            f"Q={quality:.0f}/{threshold:.0f}"
            f"{penalty_tag}"
        )
        if bbi != 0.0:
            label += f"  BBI={bbi:+.2f}"

        p.setPen(QPen(edge_color))
        p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))

        if rect_h >= 18:
            p.drawText(
                QRectF(self.pad_left + 6, y_top_c, plot_w - 12, rect_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
        else:
            label_y = max(y_top_c - 3, self.pad_top + 12)
            p.drawText(int(self.pad_left + 6), int(label_y), label)


class CandlestickDialog(QDialog):
    """Legacy dialog wrapper."""

    def __init__(self, parent=None, symbol_id: str = "") -> None:
        super().__init__(parent)
        cfg = _candle_cfg()
        theme = config.ui.theme

        self.setWindowTitle("Candlestick Chart")
        self.resize(1400, 800)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        label_style = f"color: {theme.text.muted}; background: transparent;"

        toolbar.addWidget(self._make_label("Symbol", label_style))
        self.symbol_combo = QComboBox()
        for s in registry.enabled():
            self.symbol_combo.addItem(s.display or s.id, s.id)
        if symbol_id:
            idx = self.symbol_combo.findData(symbol_id)
            if idx >= 0:
                self.symbol_combo.setCurrentIndex(idx)
        self.symbol_combo.setFixedWidth(140)
        toolbar.addWidget(self.symbol_combo)

        toolbar.addWidget(self._make_label("Interval", label_style))
        self.interval_combo = QComboBox()
        for iv in cfg.get("available_intervals", ["1m", "5m", "15m", "1h", "4h", "1d"]):
            self.interval_combo.addItem(iv, iv)
        default_iv = str(cfg.get("default_interval", "5m"))
        idx = self.interval_combo.findData(default_iv)
        if idx >= 0:
            self.interval_combo.setCurrentIndex(idx)
        self.interval_combo.setFixedWidth(80)
        toolbar.addWidget(self.interval_combo)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.setObjectName("Ghost")
        self.reload_btn.clicked.connect(self._reload)
        toolbar.addWidget(self.reload_btn)

        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        initial_symbol = symbol_id or (self.symbol_combo.currentData() or "")
        initial_interval = self.interval_combo.currentData() or "5m"

        self.canvas = CandlestickCanvas(
            symbol_id=initial_symbol,
            interval=initial_interval,
            target_days=7,
        )
        layout.addWidget(self.canvas, 1)

        self.symbol_combo.currentIndexChanged.connect(self._reload)
        self.interval_combo.currentIndexChanged.connect(self._reload)

    @staticmethod
    def _make_label(text: str, style: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(style)
        return lbl

    def _reload(self, *_args) -> None:
        symbol = self.symbol_combo.currentData() or ""
        interval = self.interval_combo.currentData() or "5m"
        self.canvas.reload(symbol, interval, target_days=7)

    def closeEvent(self, event) -> None:
        try:
            self.canvas.cleanup()
        except Exception:
            pass
        super().closeEvent(event)