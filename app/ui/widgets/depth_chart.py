from __future__ import annotations

import bisect
import time
from collections import deque

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from app.core.config import config
from app.core.event_bus import bus
from app.ui.base_widget import BaseWidget
from app.ui.theme import qcolor


class DepthChart(BaseWidget):
    widget_id = "depth_chart"

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        cfg = self.cfg
        self.levels: int = int(cfg.get("levels_to_show", 1000))
        self.price_dp: int = int(cfg.get("price_decimals", 2))
        self.qty_dp: int = int(cfg.get("qty_decimals", 4))
        self.show_grid: bool = bool(cfg.get("show_grid", True))
        self.show_axis_labels: bool = bool(cfg.get("show_axis_labels", True))
        self.show_totals: bool = bool(cfg.get("show_totals", True))
        self.fill_opacity: float = float(cfg.get("fill_opacity", 0.15))
        self.line_opacity: float = float(cfg.get("line_opacity", 0.95))
        self.mid_line_width: int = int(cfg.get("mid_line_width", 1))
        self.subscribe_direct: bool = bool(cfg.get("subscribe_direct", True))

        self.redraw_hz: int = int(cfg.get("redraw_hz", 10))
        self.axis_window_sec: float = float(cfg.get("axis_window_sec", 3.0))
        self.axis_mid_smooth_alpha: float = float(cfg.get("axis_mid_smooth_alpha", 0.15))

        # Default range: ±0.15% (usually fills the screen nicely with 1000 levels)
        self._default_range_pct: float = float(cfg.get("axis_price_range_pct", 0.0015))
        self._current_range_pct: float = self._default_range_pct

        # Default Y-axis scale
        self._default_y_scale: float = float(cfg.get("axis_y_scale", 1.0))
        self._current_y_scale: float = self._default_y_scale

        self.zoom_min_pct: float = float(cfg.get("zoom_min_pct", 0.0001))
        self.zoom_max_pct: float = float(cfg.get("zoom_max_pct", 0.02))
        self.zoom_step: float = float(cfg.get("zoom_step", 1.15))
        self.zoom_y_min: float = float(cfg.get("zoom_y_min", 0.10))
        self.zoom_y_max: float = float(cfg.get("zoom_y_max", 10.00))
        self.show_zoom_indicator: bool = bool(cfg.get("show_zoom_indicator", True))

        self.crosshair_enabled: bool = bool(cfg.get("crosshair_enabled", True))
        self.crosshair_dash: bool = bool(cfg.get("crosshair_dash", True))
        self.crosshair_marker_radius: int = int(cfg.get("crosshair_marker_radius", 4))
        self.crosshair_tooltip: bool = bool(cfg.get("crosshair_tooltip", True))
        self.crosshair_tooltip_width: int = int(cfg.get("crosshair_tooltip_width", 170))
        self.crosshair_tooltip_height: int = int(cfg.get("crosshair_tooltip_height", 46))

        self._bids: list[tuple[float, float]] = []
        self._asks: list[tuple[float, float]] = []
        self._mouse_pos: QPoint | None = None
        self._dirty: bool = False
        self._alive: bool = True
        self.destroyed.connect(self._on_destroyed)

        # Smoothed max cumulative volume for the Y-axis
        self._smoothed_max_cum: float = 1.0
        self._axis_mid: float | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / max(self.redraw_hz, 1)))
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        topic_key = "feed_depth" if self.subscribe_direct else "book_update"
        self._topic_book = config.protocol.topics[topic_key]
        bus.subscribe(self._topic_book, self._on_book)

    def _on_destroyed(self, *_args) -> None:
        self._alive = False

    def cleanup(self) -> None:
        self._alive = False
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            bus.unsubscribe(self._topic_book, self._on_book)
        except Exception:
            pass

    async def _on_book(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        book = payload.get("data") or payload.get("book") or payload
        
        raw_bids = book.get("bids", [])
        raw_asks = book.get("asks", [])
        
        self._bids = [(float(p), float(q)) for p, q in raw_bids[: self.levels]]
        self._asks = [(float(p), float(q)) for p, q in raw_asks[: self.levels]]

        self._update_axis_mid()
        self._dirty = True

    def _update_axis_mid(self) -> None:
        if not self._bids or not self._asks:
            return
        best_bid = self._bids[0][0]
        best_ask = self._asks[0][0]
        mid = (best_bid + best_ask) / 2.0

        if self._axis_mid is None:
            self._axis_mid = mid
        else:
            a = self.axis_mid_smooth_alpha
            self._axis_mid = a * mid + (1.0 - a) * self._axis_mid

    def _tick(self) -> None:
        if not self._alive:
            return
        if self._dirty:
            self._dirty = False
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = event.position().toPoint()
        self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._mouse_pos = None
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.reset_zoom()
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Home:
            self.reset_zoom()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self._alive:
            event.ignore()
            return

        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return

        notches = delta / 120.0
        factor = self.zoom_step ** (-notches)

        mods = event.modifiers()
        
        # Ctrl or Shift + Scroll zooms Y (Volume). Regular scroll zooms X (Price).
        if mods & (Qt.ControlModifier | Qt.ShiftModifier):
            new_scale = self._current_y_scale * factor
            self._current_y_scale = max(self.zoom_y_min, min(self.zoom_y_max, new_scale))
        else:
            new_pct = self._current_range_pct * factor
            self._current_range_pct = max(self.zoom_min_pct, min(self.zoom_max_pct, new_pct))

        self.update()
        event.accept()

    def reset_zoom(self) -> None:
        self._current_range_pct = self._default_range_pct
        self._current_y_scale = self._default_y_scale
        self.update()

    @staticmethod
    def _lookup_cum(prices: list[float], cums: list[float], target: float) -> float:
        if not prices:
            return 0.0
        idx = bisect.bisect_right(prices, target) - 1
        if idx < 0:
            return 0.0
        return cums[idx]

    def _calculate_visible_max_cum(self, p_min: float, p_max: float) -> float:
        if not self._bids or not self._asks:
            return 1.0

        bid_cum = 0.0
        for price, qty in self._bids:
            if price < p_min:
                break
            bid_cum += qty

        ask_cum = 0.0
        for price, qty in self._asks:
            if price > p_max:
                break
            ask_cum += qty

        return max(bid_cum, ask_cum, 1e-9)

    def paintEvent(self, _event) -> None:  # noqa: N802
        W = self.width()
        H = self.height()
        if W < 60 or H < 60 or not self._bids or not self._asks:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        theme = config.ui.theme

        # Plot area is the full widget width. 
        # We reserve the bottom 22px for the price labels so they don't get cut off.
        plot_w = W
        plot_h = H - 22 if self.show_axis_labels else H

        if self._axis_mid is None:
            return

        half_width = self._axis_mid * self._current_range_pct
        p_min = self._axis_mid - half_width
        p_max = self._axis_mid + half_width
        if p_max <= p_min:
            return

        target_max_cum = self._calculate_visible_max_cum(p_min, p_max)
        
        if self._smoothed_max_cum <= 0:
            self._smoothed_max_cum = target_max_cum
        else:
            self._smoothed_max_cum = (0.85 * self._smoothed_max_cum) + (0.15 * target_max_cum)

        max_cum = self._smoothed_max_cum * self._current_y_scale
        if max_cum <= 0:
            max_cum = 1.0

        def x_of(price: float) -> float:
            return (price - p_min) / (p_max - p_min) * plot_w

        def y_of(c: float) -> float:
            return plot_h - (c / max_cum) * plot_h

        def price_of(x_screen: float) -> float:
            frac = max(0.0, min(1.0, x_screen / plot_w))
            return p_min + frac * (p_max - p_min)

        if self.show_grid:
            grid = qcolor(theme.borders.subtle)
            p.setPen(QPen(grid, 1, Qt.DotLine))
            for i in range(1, 4):
                y = plot_h * i / 4
                p.drawLine(0, int(y), W, int(y))
            for i in range(1, 4):
                x = plot_w * i / 4
                p.drawLine(int(x), 0, int(x), int(plot_h))

        # ─── DRAW BIDS (GREEN) ───
        bid_path = QPainterPath()
        cum = 0.0
        bid_total = 0.0
        bid_xs = []
        bid_ys = []
        
        if self._bids:
            best_bid = self._bids[0][0]
            bid_path.moveTo(x_of(best_bid), y_of(0))
            
            for price, qty in self._bids:
                if price < p_min:
                    break
                
                prev_cum = cum
                cum += qty
                bid_total = cum
                
                # Step 1: horizontal line to the new price
                bid_path.lineTo(x_of(price), y_of(prev_cum))
                # Step 2: vertical line dropping the new volume
                bid_path.lineTo(x_of(price), y_of(cum))
                
                # Store ascending for crosshair lookup
                bid_xs.insert(0, price)
                bid_ys.insert(0, cum)
                
            # Extend flat to the left edge (0)
            bid_path.lineTo(x_of(p_min), y_of(cum))
            bid_path.lineTo(x_of(p_min), y_of(0))
            bid_path.closeSubpath()

            fill = QColor(theme.semantic.up)
            fill.setAlphaF(self.fill_opacity)
            p.fillPath(bid_path, fill)
            line = QColor(theme.semantic.up)
            line.setAlphaF(self.line_opacity)
            p.setPen(QPen(line, 2))
            p.drawPath(bid_path)

        # ─── DRAW ASKS (RED) ───
        ask_path = QPainterPath()
        cum = 0.0
        ask_total = 0.0
        ask_xs = []
        ask_ys = []
        
        if self._asks:
            best_ask = self._asks[0][0]
            ask_path.moveTo(x_of(best_ask), y_of(0))
            
            for price, qty in self._asks:
                if price > p_max:
                    break
                
                prev_cum = cum
                cum += qty
                ask_total = cum
                
                # Step 1: horizontal line to the new price
                ask_path.lineTo(x_of(price), y_of(prev_cum))
                # Step 2: vertical line rising with new volume
                ask_path.lineTo(x_of(price), y_of(cum))
                
                # Store ascending (already ascending)
                ask_xs.append(price)
                ask_ys.append(cum)
                
            # Extend flat to the right edge (W)
            ask_path.lineTo(x_of(p_max), y_of(cum))
            ask_path.lineTo(x_of(p_max), y_of(0))
            ask_path.closeSubpath()

            fill = QColor(theme.semantic.down)
            fill.setAlphaF(self.fill_opacity)
            p.fillPath(ask_path, fill)
            line = QColor(theme.semantic.down)
            line.setAlphaF(self.line_opacity)
            p.setPen(QPen(line, 2))
            p.drawPath(ask_path)

        # ─── Mid line ───
        mid = (self._bids[0][0] + self._asks[0][0]) / 2.0
        mid_x = x_of(mid)
        mid_color = QColor(theme.text.primary)
        mid_color.setAlpha(140)
        p.setPen(QPen(mid_color, self.mid_line_width, Qt.DashLine))
        p.drawLine(int(mid_x), 0, int(mid_x), int(plot_h))

        # ─── Crosshair ───
        if (
            self.crosshair_enabled
            and self._mouse_pos is not None
            and 0 <= self._mouse_pos.x() <= plot_w
            and 0 <= self._mouse_pos.y() <= plot_h
        ):
            mx = self._mouse_pos.x()
            my = self._mouse_pos.y()
            cursor_price = price_of(mx)

            bid_cum = self._lookup_cum(bid_xs, bid_ys, cursor_price)
            ask_cum = self._lookup_cum(ask_xs, ask_ys, cursor_price)

            if cursor_price <= mid:
                hover_side = "bid"
                hover_cum = bid_cum
                anchor_price = self._nearest_price(bid_xs, cursor_price)
            else:
                hover_side = "ask"
                hover_cum = ask_cum
                anchor_price = self._nearest_price(ask_xs, cursor_price)

            hover_y_cum = (plot_h - my) / plot_h * max_cum
            hover_y_cum = max(0.0, min(max_cum, hover_y_cum))

            guide = QColor(theme.text.muted)
            guide.setAlpha(200)
            pen_style = Qt.DashLine if self.crosshair_dash else Qt.SolidLine
            p.setPen(QPen(guide, 1, pen_style))
            p.drawLine(mx, 0, mx, int(plot_h))
            p.drawLine(0, my, W, my)

            if hover_cum is not None:
                cx = x_of(anchor_price)
                cy = y_of(hover_cum)
                accent = QColor(theme.semantic.up if hover_side == "bid" else theme.semantic.down)
                r = self.crosshair_marker_radius
                p.setBrush(accent)
                p.setPen(QPen(QColor(theme.surfaces.l2), 2))
                p.drawEllipse(QPoint(int(cx), int(cy)), r, r)

            p.setPen(QPen(QColor(theme.text.primary)))
            p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))
            label = f"{cursor_price:.{self.price_dp}f}"
            rect = QRectF(mx - 45, plot_h + 2, 90, 16)
            p.drawText(rect, Qt.AlignCenter, label)

            label_cum = f"{hover_y_cum:.{self.qty_dp}f}"
            rect = QRectF(4, my - 8, 60, 16)
            p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, label_cum)

            if self.crosshair_tooltip and hover_cum is not None:
                self._draw_tooltip(p, theme, W, H, mx, my, cursor_price, hover_cum, hover_side or "bid")

        # ─── Axis & Labels ───
        if self.show_axis_labels:
            label_color = QColor(theme.text.muted)
            p.setPen(QPen(label_color))
            p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))

            # Bottom axis labels
            p.drawText(QRectF(4, plot_h + 2, 110, 16), Qt.AlignLeft | Qt.AlignVCenter, f"{p_min:.{self.price_dp}f}")
            p.drawText(QRectF(plot_w / 2.0 - 55, plot_h + 2, 110, 16), Qt.AlignCenter, f"{mid:.{self.price_dp}f}")
            p.drawText(QRectF(plot_w - 114, plot_h + 2, 110, 16), Qt.AlignRight | Qt.AlignVCenter, f"{p_max:.{self.price_dp}f}")

            # Y-axis labels (top-left, bottom-left)
            p.drawText(
                QRectF(4, 4, 80, 14),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{max_cum:.{self.qty_dp}f}",
            )
            p.drawText(
                QRectF(4, plot_h - 18, 80, 14),
                Qt.AlignLeft | Qt.AlignVCenter,
                "0",
            )

            if self.show_totals:
                p.setPen(QPen(QColor(theme.semantic.up)))
                p.drawText(
                    QRectF(8, 24, 160, 14),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    f"bids: {bid_total:.{self.qty_dp}f}",
                )
                p.setPen(QPen(QColor(theme.semantic.down)))
                p.drawText(
                    QRectF(W - 168, 24, 160, 14),
                    Qt.AlignRight | Qt.AlignVCenter,
                    f"asks: {ask_total:.{self.qty_dp}f}",
                )

        if self.show_zoom_indicator:
            pct_display = self._current_range_pct * 100.0
            x_text = f"±{pct_display:.3f}%"
            y_text_label = f"y {self._current_y_scale:.2f}×"
            full_text = f"{x_text}   {y_text_label}"

            indicator_color = QColor(theme.text.muted)
            indicator_color.setAlpha(180)
            p.setPen(QPen(indicator_color))
            p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))
            
            y_offset = 44 if self.show_totals else 24
            
            p.drawText(
                QRectF(8, y_offset, 200, 16),
                Qt.AlignLeft | Qt.AlignVCenter,
                full_text,
            )

    @staticmethod
    def _nearest_price(sorted_prices: list[float], target: float) -> float:
        if not sorted_prices:
            return target
        idx = bisect.bisect_left(sorted_prices, target)
        if idx <= 0:
            return sorted_prices[0]
        if idx >= len(sorted_prices):
            return sorted_prices[-1]
        before = sorted_prices[idx - 1]
        after = sorted_prices[idx]
        return before if abs(target - before) <= abs(after - target) else after

    def _draw_tooltip(self, p, theme, W, H, mx, my, price, cum, side) -> None:
        box_w = self.crosshair_tooltip_width
        box_h = self.crosshair_tooltip_height
        pad = 8
        offset_x = 14
        offset_y = -box_h // 2 - 4

        if mx + offset_x + box_w > W - 4:
            offset_x = -box_w - 14
        top = max(4, min(H - box_h - 4, my + offset_y))
        left = mx + offset_x

        rect = QRectF(left, top, box_w, box_h)

        bg = qcolor(theme.surfaces.l3, 240)
        p.setBrush(bg)
        p.setPen(QPen(qcolor(theme.borders.normal), 1))
        p.drawRoundedRect(rect, 6, 6)

        p.setFont(QFont(theme.fonts.mono, theme.fonts.size_sm))

        p.setPen(QPen(QColor(theme.text.primary)))
        p.drawText(
            QRectF(left + pad, top + 4, box_w - 2 * pad, 14),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"Price   {price:.{self.price_dp}f}",
        )

        side_color = QColor(theme.semantic.up if side == "bid" else theme.semantic.down)
        p.setPen(QPen(side_color))
        p.drawText(
            QRectF(left + pad, top + 22, box_w - 2 * pad, 16),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"Cum {side.upper():<4s}  {cum:.{self.qty_dp}f}",
        )