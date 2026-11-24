from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.config import config


class SidebarButton(QFrame):
    clicked = Signal()

    def __init__(self, icon: str, label: str) -> None:
        super().__init__()
        self.setObjectName("SidebarButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(40)  # Responsive height
        self.setProperty("active", "false")
        self.setProperty("hover", "false")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(0)

        self._icon = QLabel(icon)
        self._icon.setObjectName("SidebarIcon")
        self._icon.setFixedWidth(56) # Keep icon width fixed for alignment
        self._icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._icon)

        self._label = QLabel(label)
        self._label.setObjectName("SidebarLabel")
        layout.addWidget(self._label)
        layout.addStretch(1)

    def set_expanded(self, expanded: bool) -> None:
        self._label.setVisible(expanded)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self._restyle()

    def enterEvent(self, event) -> None:
        self.setProperty("hover", "true")
        self._restyle()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.setProperty("hover", "false")
        self._restyle()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def _restyle(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)


class Sidebar(QWidget):
    item_clicked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Sidebar")

        cfg = config.ui.sidebar
        self._pinned = bool(cfg.pinned_by_default)
        self._collapsed_w = cfg.collapsed_width
        self._expanded_w = cfg.expanded_width

        self._buttons: dict[str, SidebarButton] = {}

        self._set_width()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 12, 0, 8)
        root.setSpacing(2)

        # ─── Brand ──────────────────────────────────────────
        brand = QWidget()
        brand.setStyleSheet("background: transparent;")
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(0, 8, 12, 16)
        bl.setSpacing(0)

        brand_icon = QLabel("◆")
        brand_icon.setObjectName("SidebarBrandIcon")
        brand_icon.setFixedWidth(56)
        brand_icon.setAlignment(Qt.AlignCenter)
        bl.addWidget(brand_icon)

        self._brand_text = QLabel("ZEPHYROS")
        self._brand_text.setObjectName("SidebarBrandText")
        bl.addWidget(self._brand_text)
        bl.addStretch(1)
        root.addWidget(brand)

        divider = QFrame()
        divider.setObjectName("SidebarDivider")
        divider.setFixedHeight(1)
        root.addWidget(divider)
        root.addSpacing(8)

        # ─── Nav items ──────────────────────────────────────
        for item in sorted(cfg.items, key=lambda i: i.order):
            btn = self._make_button(item.id, item.icon, item.label)
            root.addWidget(btn)

        root.addStretch(1)

        # ─── Footer divider ─────────────────────────────────
        divider2 = QFrame()
        divider2.setObjectName("SidebarDivider")
        divider2.setFixedHeight(1)
        root.addWidget(divider2)
        root.addSpacing(8)

        # ─── Footer items ───────────────────────────────────
        for item in cfg.footer_items:
            btn = self._make_button(item.id, item.icon, item.label)
            root.addWidget(btn)

        # ─── Pin toggle ─────────────────────────────────────
        self._pin_btn = QToolButton()
        self._pin_btn.setObjectName("SidebarPin")
        self._pin_btn.setMinimumHeight(32)
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.clicked.connect(self._toggle_pin)
        root.addWidget(self._pin_btn)

        self._apply_pin_state()

    def set_active(self, page_id: str) -> None:
        for pid, btn in self._buttons.items():
            btn.set_active(pid == page_id)

    def _make_button(self, item_id: str, icon: str, label: str) -> SidebarButton:
        btn = SidebarButton(icon, label)
        btn.clicked.connect(lambda iid=item_id: self.item_clicked.emit(iid))
        btn.setToolTip(label)
        self._buttons[item_id] = btn
        return btn

    def _toggle_pin(self) -> None:
        self._pinned = not self._pinned
        self._apply_pin_state()

    def _apply_pin_state(self) -> None:
        self._set_width()
        self._brand_text.setVisible(self._pinned)
        for btn in self._buttons.values():
            btn.set_expanded(self._pinned)
            if self._pinned:
                btn.setToolTip("")
        self._pin_btn.setText("«" if self._pinned else "»")
        self._pin_btn.setToolTip(
            "Collapse sidebar" if self._pinned else "Expand sidebar"
        )

    def _set_width(self) -> None:
        # Sidebar width must be fixed to align the content area
        self.setFixedWidth(
            self._expanded_w if self._pinned else self._collapsed_w
        )