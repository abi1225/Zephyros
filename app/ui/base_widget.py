from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.config import config


def _glow(alpha: int) -> QColor:
    """Panel glow color derived from theme.semantic.up at given alpha."""
    c = QColor(config.ui.theme.semantic.up)
    c.setAlpha(alpha)
    return c


class Panel(QFrame):
    """Glassmorphic Acrylic Card chrome with dynamic hover glow."""

    def __init__(self, title: str | None, body: QWidget, collapsible: bool = False) -> None:
        super().__init__()
        self.setObjectName("Panel")

        L = config.ui.layout
        theme = config.ui.theme
        self._collapsible = collapsible
        self._collapsed = False
        self._body_wrapper: QWidget | None = None
        self._toggle_btn: QToolButton | None = None
        self._header_h = L.panel_header_height
        self._shadow: QGraphicsDropShadowEffect | None = None

        # Neo-Luminous Acrylic Style — background derives from
        # theme.surfaces.l2 at 75% opacity.
        base = QColor(theme.surfaces.l2)
        bg_css = (
            f"rgba({base.red()}, {base.green()}, {base.blue()}, 0.75)"
        )
        self.setStyleSheet(f"""
        QFrame#Panel {{
            background-color: {bg_css};
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-top: 1px solid rgba(255, 255, 255, 0.16);
            border-radius: {L.panel_radius}px;
        }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if title:
            header = QWidget()
            header.setFixedHeight(self._header_h)
            header.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(header)
            hl.setContentsMargins(L.panel_padding, 0, L.panel_padding, 0)
            hl.setSpacing(6)

            if collapsible:
                self._toggle_btn = QToolButton()
                self._toggle_btn.setText("▾")
                self._toggle_btn.setCursor(Qt.PointingHandCursor)
                self._toggle_btn.setFixedSize(16, 16)
                self._toggle_btn.setStyleSheet(
                    f"color: {config.ui.theme.text.muted}; "
                    f"background: transparent; border: none; font-size: 14px;"
                )
                self._toggle_btn.clicked.connect(self._toggle_collapse)
                hl.addWidget(self._toggle_btn)

            label = QLabel(title)
            label.setObjectName("PanelTitle")
            hl.addWidget(label)
            hl.addStretch(1)

            if collapsible:
                header.mouseReleaseEvent = self._header_clicked
                header.setCursor(Qt.PointingHandCursor)

            layout.addWidget(header)

            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"background-color: {config.ui.theme.borders.subtle}; border: none;")
            layout.addWidget(sep)

        # Body wrapper
        wrapper = QWidget()
        wrapper.setStyleSheet("background: transparent;")
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(body)
        layout.addWidget(wrapper, 1)

        self._body_wrapper = wrapper

        # Faint Neon Glow (Default)
        if L.panel_shadow:
            self._shadow = QGraphicsDropShadowEffect(self)
            self._shadow.setBlurRadius(24)
            self._shadow.setXOffset(0)
            self._shadow.setYOffset(0)
            self._shadow.setColor(_glow(30))
            self.setGraphicsEffect(self._shadow)

    # ─── Hover Glow ─────────────────────────────────────────

    def enterEvent(self, event) -> None:
        if self._shadow is not None:
            self._shadow.setBlurRadius(36)
            self._shadow.setColor(_glow(90))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._shadow is not None:
            self._shadow.setBlurRadius(24)
            self._shadow.setColor(_glow(30))
        super().leaveEvent(event)

    # ─── Collapse Logic ─────────────────────────────────────

    def _header_clicked(self, event) -> None:
        if self._collapsible and event.button() == Qt.LeftButton:
            self._toggle_collapse()

    def _toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed

        if self._body_wrapper is not None:
            self._body_wrapper.setVisible(not self._collapsed)

        if self._toggle_btn is not None:
            self._toggle_btn.setText("▸" if self._collapsed else "▾")

        if self._collapsed:
            self.setMaximumHeight(self._header_h + 1)
        else:
            self.setMaximumHeight(16777215)


class BaseWidget(QWidget):
    """All widgets subclass this. Provides config access to the
    widget's own block from ui.widgets.<id>."""

    widget_id: str = "unnamed"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        widget_cfg = config.ui.widgets.get(self.widget_id)
        self.cfg: dict = widget_cfg.model_dump() if widget_cfg is not None else {}

    def cfg_get(self, key: str, default=None):
        return self.cfg.get(key, default)