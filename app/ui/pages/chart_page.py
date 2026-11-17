from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.config import config
from app.core.symbols import registry
from app.ui.base_widget import Panel
from app.ui.dialogs.candlestick import CandlestickCanvas
from app.ui.widgets.custom import ComboBox


class ChartPage(QWidget):
    """Candlestick chart as a full page with its own toolbar."""

    def __init__(self) -> None:
        super().__init__()
        cfg = config.ui.candlestick or {}
        theme = config.ui.theme
        L = config.ui.layout

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            L.content_margins, L.content_margins,
            L.content_margins, L.content_margins,
        )
        layout.setSpacing(L.content_spacing)

        # ─── Toolbar ────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.setSpacing(12)
        label_style = f"color: {theme.text.muted}; background: transparent;"

        toolbar.addWidget(self._label("Symbol", label_style))
        self.symbol_combo = ComboBox()
        for s in registry.enabled():
            self.symbol_combo.addItem(s.display or s.id, s.id)
        self.symbol_combo.setMinimumWidth(130)
        self.symbol_combo.setMaximumWidth(200)
        self.symbol_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        toolbar.addWidget(self.symbol_combo)

        toolbar.addWidget(self._label("Interval", label_style))
        self.interval_combo = ComboBox()
        available = list(
            cfg.get(
                "available_intervals",
                ["1m", "5m", "15m", "1h", "4h", "1d"],
            )
        )
        for iv in available:
            self.interval_combo.addItem(iv, iv)
        
        # CHANGED FALLBACK TO 5M
        default_iv = str(cfg.get("default_interval", "5m"))
        idx = self.interval_combo.findData(default_iv)
        if idx >= 0:
            self.interval_combo.setCurrentIndex(idx)
            
        self.interval_combo.setMinimumWidth(80)
        self.interval_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        toolbar.addWidget(self.interval_combo)

        self.reload_btn = QPushButton("⟳  Reload")
        self.reload_btn.setObjectName("Ghost")
        self.reload_btn.setMinimumHeight(32)
        self.reload_btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.reload_btn.clicked.connect(self._reload)
        toolbar.addWidget(self.reload_btn)

        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        # ─── Chart canvas inside a panel ────────────────────
        initial_symbol = self.symbol_combo.currentData() or ""
        initial_interval = self.interval_combo.currentData() or "5m"

        self.canvas = CandlestickCanvas(
            symbol_id=initial_symbol,
            interval=initial_interval,
            target_days=7,
        )
        
        chart_panel = Panel("Chart", self.canvas)
        layout.addWidget(chart_panel, 1)

        self.symbol_combo.currentIndexChanged.connect(self._reload)
        self.interval_combo.currentIndexChanged.connect(self._reload)

    @staticmethod
    def _label(text: str, style: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(style)
        return lbl

    def _reload(self, *_args) -> None:
        symbol = self.symbol_combo.currentData() or ""
        interval = self.interval_combo.currentData() or "5m"
        self.canvas.reload(symbol, interval, target_days=7)

    def cleanup(self) -> None:
        try:
            self.canvas.cleanup()
        except Exception:
            pass