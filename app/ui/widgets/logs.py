from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.config import config
from app.core.event_bus import bus


class LogsWidget(QWidget):
    """Collapsible event stream drawer."""

    widget_id = "logs"

    def __init__(self) -> None:
        super().__init__()

        self._alive = True
        self._expanded = bool(config.ui.drawer.expanded_by_default)
        self._timestamps: deque = deque(maxlen=2000)
        self._pending_logs: list[str] = []

        cfg = config.ui.drawer
        t = config.ui.theme
        s = t.surfaces
        b = t.borders
        f = t.fonts

        self._collapsed_h = cfg.collapsed_height
        self._expanded_h = cfg.expanded_height

        self.setStyleSheet(f"""
        QWidget#LogsDrawer {{
            background-color: {s.l1};
            border-top: 1px solid {b.subtle};
        }}
        QToolButton#LogsHeader {{
            background: transparent;
            border: none;
            color: {t.text.secondary};
            padding: 0px;
            text-align: left;
            font-size: {f.size_sm}px;
            font-weight: 600;
            letter-spacing: 0.5px;
        }}
        QToolButton#LogsHeader:hover {{
            color: {t.text.primary};
        }}
        QLabel#LogsRate {{
            color: {t.text.muted};
            font-size: {f.size_sm}px;
            background: transparent;
        }}
        """)

        self.setObjectName("LogsDrawer")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ─── Header ─────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(self._collapsed_h)
        header.setStyleSheet("background: transparent;")
        header.setCursor(Qt.PointingHandCursor)
        header.mouseReleaseEvent = self._header_clicked  # Make whole header clickable
        
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)
        hl.setSpacing(10)

        self._chevron = QToolButton()
        self._chevron.setObjectName("LogsHeader")
        self._chevron.setText("▸")
        self._chevron.setCursor(Qt.PointingHandCursor)
        self._chevron.setFixedWidth(16)
        self._chevron.clicked.connect(self._toggle)
        hl.addWidget(self._chevron)

        self._title = QToolButton()
        self._title.setObjectName("LogsHeader")
        self._title.setText(cfg.title)
        self._title.setCursor(Qt.PointingHandCursor)
        self._title.clicked.connect(self._toggle)
        hl.addWidget(self._title)

        hl.addStretch(1)

        self._rate = QLabel("0 msg/min")
        self._rate.setObjectName("LogsRate")
        hl.addWidget(self._rate)

        root.addWidget(header)

        # ─── Separator ──────────────────────────────────────
        self._sep = QFrame()
        self._sep.setFixedHeight(1)
        self._sep.setStyleSheet(f"background-color: {config.ui.theme.borders.subtle}; border: none;")
        root.addWidget(self._sep)

        # ─── Body ───────────────────────────────────────────
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        
        # Safely get max_rows
        log_cfg = config.ui.widgets.get("logs")
        max_rows = 500
        if log_cfg and hasattr(log_cfg, "max_rows"):
            max_rows = getattr(log_cfg, "max_rows", 500)
        elif log_cfg and "max_rows" in getattr(log_cfg, "model_extra", {}):
            max_rows = log_cfg.model_extra["max_rows"]
            
        self._view.setMaximumBlockCount(max_rows)
        root.addWidget(self._view, 1)

        bus.subscribe(config.protocol.topics["log_emit"], self._on_log)

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(250)  # 4 Hz batching
        self._flush_timer.timeout.connect(self._flush_logs)
        self._flush_timer.start()

        self._apply_expanded()

    # ─── Public ─────────────────────────────────────────────

    def cleanup(self) -> None:
        self._alive = False
        try:
            self._flush_timer.stop()
            bus.unsubscribe(config.protocol.topics["log_emit"], self._on_log)
        except Exception:
            pass

    # ─── Internals ──────────────────────────────────────────

    def _header_clicked(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._toggle()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._apply_expanded()

    def _apply_expanded(self) -> None:
        if self._expanded:
            self.setFixedHeight(self._expanded_h)
            self._view.setVisible(True)
            self._sep.setVisible(True)
            self._chevron.setText("▾")
        else:
            self.setFixedHeight(self._collapsed_h)
            self._view.setVisible(False)
            self._sep.setVisible(False)
            self._chevron.setText("▸")

    def _flush_logs(self) -> None:
        if not self._alive or not self._pending_logs:
            return
        self._view.appendPlainText("\n".join(self._pending_logs))
        self._pending_logs.clear()
        self._update_rate()

    def _update_rate(self) -> None:
        now = time.time()
        cutoff = now - 60
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        self._rate.setText(f"{len(self._timestamps)} msg/min")

    async def _on_log(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        level = str(payload.get("level", "info")).upper()
        module = str(payload.get("module", "-"))
        msg = str(payload.get("message", ""))
        line = f"[{level:5s}] [{module}] {msg}"
        
        self._pending_logs.append(line)
        self._timestamps.append(time.time())