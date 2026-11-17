from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox

from app.core.config import config


class ComboBox(QComboBox):
    """QComboBox that paints its own arrow. Works regardless of QSS/native style."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        theme = config.ui.theme
        bg = QColor(theme.surfaces.l2)
        arrow = QColor(theme.text.secondary)
        line = QColor(theme.borders.subtle)

        w = 24
        # Erase whatever QSS/native drew in the arrow area
        painter.fillRect(self.width() - w, 1, w - 1, self.height() - 2, bg)

        # Vertical separator on the left of the arrow area
        painter.setPen(QPen(line, 1))
        painter.drawLine(self.width() - w, 1, self.width() - w, self.height() - 1)

        # Draw the triangle
        cx = self.width() - 12
        cy = self.height() // 2
        painter.setBrush(arrow)
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF([
            QPointF(cx - 5, cy - 2),
            QPointF(cx + 5, cy - 2),
            QPointF(cx, cy + 3),
        ]))
        painter.end()


class LotInput(QDoubleSpinBox):
    """QDoubleSpinBox that paints its own up/down arrows."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        theme = config.ui.theme
        bg = QColor(theme.surfaces.l2)
        arrow = QColor(theme.text.secondary)
        line = QColor(theme.borders.subtle)

        w = 20
        # Erase the spinner area
        painter.fillRect(self.width() - w, 1, w - 1, self.height() - 2, bg)

        # Separators
        painter.setPen(QPen(line, 1))
        painter.drawLine(self.width() - w, 1, self.width() - w, self.height() - 1)
        painter.drawLine(
            self.width() - w, self.height() // 2,
            self.width() - 1, self.height() // 2,
        )

        # Draw up + down triangles
        cx = self.width() - w // 2
        top_cy = self.height() // 4
        bot_cy = 3 * self.height() // 4
        painter.setBrush(arrow)
        painter.setPen(Qt.NoPen)

        painter.drawPolygon(QPolygonF([
            QPointF(cx - 4, top_cy + 2),
            QPointF(cx + 4, top_cy + 2),
            QPointF(cx, top_cy - 3),
        ]))
        painter.drawPolygon(QPolygonF([
            QPointF(cx - 4, bot_cy - 2),
            QPointF(cx + 4, bot_cy - 2),
            QPointF(cx, bot_cy + 3),
        ]))
        painter.end()