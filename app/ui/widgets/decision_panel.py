from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
)

from app.core.config import config
from app.core.event_bus import bus
from app.modules.strategy.schemas import get_schema
from app.ui.base_widget import BaseWidget


# ─── State label mapping ────────────────────────────────────

_STATE_LABEL = {
    "flat": "FLAT",
    "pending_entry": "PEND",
    "in_position": "OPEN",
    "contacting": "CONTACT",
    "confirming": "CONFIRM",
    "armed": "ARMED",
    "watching": "WATCH",
    "warming_up": "WARM",
}

_WAIT_STATES = {
    "pending_entry", "contacting", "confirming",
    "watching", "armed", "warming_up",
}


# ─── Formatters ─────────────────────────────────────────────

def _fmt_time(v: Any) -> str:
    try:
        return datetime.fromtimestamp(int(v) / 1000).strftime("%H:%M:%S")
    except Exception:
        return ""


def _fmt_price(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except Exception:
        return ""


def _fmt_state(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).lower()
    return _STATE_LABEL.get(s, str(v).upper())


def _fmt_x_mult(v: Any) -> str:
    try:
        return f"{float(v):.1f}x"
    except Exception:
        return ""


def _fmt_pct(v: Any) -> str:
    try:
        return f"{float(v):+.1f}%"
    except Exception:
        return ""


def _fmt_signed_1dp(v: Any) -> str:
    try:
        return f"{float(v):+.1f}"
    except Exception:
        return ""


def _fmt_signed_2dp(v: Any) -> str:
    try:
        return f"{float(v):+.2f}"
    except Exception:
        return ""


def _fmt_signed_4dp(v: Any) -> str:
    try:
        return f"{float(v):+.4f}"
    except Exception:
        return ""


def _fmt_signed_int(v: Any) -> str:
    try:
        return f"{float(v):+.0f}"
    except Exception:
        return ""


def _fmt_raw(v: Any) -> str:
    return "" if v is None else str(v)


def _fmt_sec_int(v: Any) -> str:
    try:
        return f"{float(v):.0f}s"
    except Exception:
        return ""


def _fmt_sec_1dp(v: Any) -> str:
    try:
        return f"{float(v):.1f}s"
    except Exception:
        return ""


def _fmt_ratio(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except Exception:
        return ""


def _fmt_vpin(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except Exception:
        return ""


_FORMATTERS: dict[str, Callable[[Any], str]] = {
    "time": _fmt_time,
    "price": _fmt_price,
    "state": _fmt_state,
    "x_mult": _fmt_x_mult,
    "pct": _fmt_pct,
    "signed_1dp": _fmt_signed_1dp,
    "signed_2dp": _fmt_signed_2dp,
    "signed_4dp": _fmt_signed_4dp,
    "signed_int": _fmt_signed_int,
    "raw": _fmt_raw,
    "sec_int": _fmt_sec_int,
    "sec_1dp": _fmt_sec_1dp,
    "ratio": _fmt_ratio,
    "vpin": _fmt_vpin,
}

# Columns whose value is numeric-ish and should be right-aligned
_RIGHT_ALIGN_FMT = {
    "price", "x_mult", "pct",
    "signed_1dp", "signed_2dp", "signed_4dp", "signed_int",
    "sec_int", "sec_1dp", "ratio", "vpin",
}


# ─── Colorizers ─────────────────────────────────────────────

def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _color_for(
    key: str, raw_value: Any, theme,
) -> tuple[QColor | None, bool]:
    """Return (foreground_color_or_None, bold)."""
    if key == "muted":
        return QColor(theme.text.muted), False

    if key == "state":
        s = str(raw_value).lower() if raw_value is not None else ""
        if s == "in_position":
            return QColor(theme.semantic.up), True
        if s in _WAIT_STATES:
            return QColor(theme.semantic.warn), True
        if s == "flat":
            return QColor(theme.text.secondary), False
        return QColor(theme.text.muted), False

    if key == "golden":
        f = _to_float(raw_value)
        if f is None:
            return QColor(theme.text.muted), False
        if 20.0 <= f <= 50.0:
            return QColor(theme.semantic.up), True
        if f > 50.0:
            return QColor(theme.semantic.down), False
        return QColor(theme.text.muted), False

    if key in ("signed", "signed_bold", "signed_small"):
        f = _to_float(raw_value)
        bold = key in ("signed", "signed_bold")
        if f is None or f == 0:
            return QColor(theme.text.secondary), bold
        return (
            QColor(theme.semantic.up) if f > 0 else QColor(theme.semantic.down),
            bold,
        )

    if key == "regime":
        r = str(raw_value) if raw_value is not None else ""
        if r == "STEAM":
            return QColor(theme.semantic.up), False
        if r == "KNIFE":
            return QColor(theme.semantic.down), False
        if r in ("STALL", "EXHAUST"):
            return QColor(theme.semantic.warn), False
        return QColor(theme.text.muted), False

    if key == "decision":
        d = str(raw_value).upper() if raw_value is not None else ""
        if d == "ENTER":
            return QColor(theme.semantic.up), True
        if d in ("SKIP", "COLLAPSE"):
            return QColor(theme.semantic.down), False
        if d == "WAIT":
            return QColor(theme.semantic.warn), False
        return QColor(theme.text.muted), False

    if key == "side":
        s = str(raw_value).lower() if raw_value is not None else ""
        if s in ("buy", "demand", "bid"):
            return QColor(theme.semantic.up), True
        if s in ("sell", "supply", "ask"):
            return QColor(theme.semantic.down), True
        return QColor(theme.text.muted), False

    if key == "vpin":
        f = _to_float(raw_value)
        if f is None:
            return QColor(theme.text.muted), False
        if f > 0.65:
            return QColor(theme.semantic.down), True
        if f > 0.50:
            return QColor(theme.semantic.warn), False
        return QColor(theme.text.secondary), False

    if key in ("retention", "retention_hold"):
        f = _to_float(raw_value)
        if f is None:
            return QColor(theme.text.muted), False
        if f >= 0.80:
            return QColor(theme.semantic.up), False
        if f <= 0.40:
            return QColor(theme.semantic.down), False
        return QColor(theme.semantic.warn), False

    return None, False


# ─── Panel ──────────────────────────────────────────────────

class DecisionPanel(BaseWidget):
    """Per-strategy debug telemetry table.

    Column layout, formatter, and colorizer are all driven by
    `STRATEGY_SCHEMAS`. On `strategy_changed` the panel rebuilds
    itself so the displayed columns match the active strategy.
    """

    widget_id = "decision_panel"
    MAX_ROWS = 100

    def __init__(self) -> None:
        super().__init__()
        self._alive = True
        self.destroyed.connect(self._on_destroyed)

        theme = config.ui.theme

        self._strategy_id: str = str(config.strategies.active or "order_flow")
        self._schema: dict[str, Any] = get_schema(self._strategy_id)
        self._columns: list[dict[str, Any]] = list(
            self._schema.get("columns", [])
        )
        self._legend_html: str = self._schema.get("legend", "")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # ─── Table ─────────────────────────────────────────
        self._table = QTableWidget(0, 0)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)
        self._table.setWordWrap(False)
        self._table.setHorizontalScrollMode(
            QAbstractItemView.ScrollPerPixel
        )
        root.addWidget(self._table, 1)

        # ─── Separator ─────────────────────────────────────
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            f"background-color: {theme.borders.subtle}; border: none;"
        )
        root.addWidget(sep)

        # ─── Legend toggle ─────────────────────────────────
        self._legend_btn = QToolButton()
        self._legend_btn.setCursor(Qt.PointingHandCursor)
        self._legend_btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                border: none;
                color: {theme.text.secondary};
                font-size: {theme.fonts.size_sm}px;
                font-weight: 600;
                padding: 4px;
                text-align: left;
            }}
            QToolButton:hover {{ color: {theme.text.primary}; }}
        """)
        self._legend_btn.clicked.connect(self._toggle_legend)
        root.addWidget(self._legend_btn)

        self._legend_box = QTextEdit()
        self._legend_box.setReadOnly(True)
        self._legend_box.setFixedHeight(190)
        self._legend_box.setStyleSheet(f"""
            QTextEdit {{
                background-color: {theme.surfaces.l1};
                color: {theme.text.secondary};
                border: 1px solid {theme.borders.subtle};
                border-radius: 4px;
                font-family: {theme.fonts.ui};
                font-size: {theme.fonts.size_sm}px;
                padding: 8px;
            }}
        """)
        self._legend_box.setVisible(False)
        root.addWidget(self._legend_box)

        # ─── Build initial layout ──────────────────────────
        self._rebuild_columns(initial=True)

        # ─── Subscriptions ─────────────────────────────────
        self._topic_debug = config.protocol.topics.get(
            "debug_decision", "debug.decision"
        )
        self._topic_strategy = config.protocol.topics.get(
            "strategy_changed", "strategy_changed"
        )
        bus.subscribe(self._topic_debug, self._on_decision)
        bus.subscribe(self._topic_strategy, self._on_strategy_changed)

    # ═══════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════

    def _on_destroyed(self, *_args) -> None:
        self._alive = False

    def cleanup(self) -> None:
        self._alive = False
        for topic, handler in (
            (self._topic_debug, self._on_decision),
            (self._topic_strategy, self._on_strategy_changed),
        ):
            try:
                bus.unsubscribe(topic, handler)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════
    # Strategy switching
    # ═══════════════════════════════════════════════════════

    async def _on_strategy_changed(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        new_id = str((payload or {}).get("strategy", "")).strip()
        if not new_id or new_id == self._strategy_id:
            return
        self._rebuild_columns(new_strategy=new_id)

    def _rebuild_columns(
        self,
        new_strategy: str | None = None,
        initial: bool = False,
    ) -> None:
        """Re-apply header labels, widths, tooltips, and legend."""
        if new_strategy is not None:
            schema = get_schema(new_strategy)
            if not schema:
                return
            self._strategy_id = new_strategy
            self._schema = schema
            self._columns = list(schema.get("columns", []))
            self._legend_html = schema.get("legend", "")

        if not self._columns:
            return

        headers = [
            c.get("label", c.get("key", "")) for c in self._columns
        ]

        # Full reset so any old rows with the previous schema vanish.
        self._table.setUpdatesEnabled(False)
        try:
            self._table.setRowCount(0)
            self._table.setColumnCount(len(headers))
            self._table.setHorizontalHeaderLabels(headers)

            header = self._table.horizontalHeader()
            for i, col in enumerate(self._columns):
                tip = col.get("tooltip")
                if tip:
                    item = self._table.horizontalHeaderItem(i)
                    if item is not None:
                        item.setToolTip(tip)

                w = col.get("width")
                if w is None:
                    header.setSectionResizeMode(i, QHeaderView.Stretch)
                else:
                    header.setSectionResizeMode(i, QHeaderView.Interactive)
                    self._table.setColumnWidth(i, int(w))
        finally:
            self._table.setUpdatesEnabled(True)
            self._table.viewport().update()

        # Legend text + button
        self._legend_box.setHtml(self._legend_html)
        self._update_legend_btn_text()

        if not initial:
            label = self._schema.get("label", self._strategy_id)
            bus.publish  # noop — kept for symmetry with old code
            # A tiny event-bus message so the log drawer shows the swap:
            import asyncio as _asyncio
            try:
                _asyncio.create_task(bus.publish(
                    config.protocol.topics["log_emit"],
                    {
                        "level": "info",
                        "module": self.widget_id,
                        "message": f"Debug view → {label}",
                    },
                ))
            except RuntimeError:
                pass

    def _update_legend_btn_text(self) -> None:
        label = self._schema.get("label", self._strategy_id)
        prefix = "▾  " if self._legend_box.isVisible() else "▸  "
        self._legend_btn.setText(
            f"{prefix}STRATEGY LEGEND — {label.upper()}"
        )

    def _toggle_legend(self) -> None:
        vis = not self._legend_box.isVisible()
        self._legend_box.setVisible(vis)
        self._update_legend_btn_text()

    # ═══════════════════════════════════════════════════════
    # Row insertion
    # ═══════════════════════════════════════════════════════

    async def _on_decision(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        # Belt-and-braces filter: if the payload carries a `strategy`
        # tag (post-installments 5-6), ignore rows that don't belong
        # to the currently-viewed strategy.
        strat = str(payload.get("strategy", "")).strip()
        if strat and strat != self._strategy_id:
            return
        self._add_row(payload)

    def _add_row(self, payload: dict) -> None:
        theme = config.ui.theme
        self._table.insertRow(0)

        for col_idx, col in enumerate(self._columns):
            key = col.get("key", "")
            fmt_key = col.get("fmt", "raw")
            color_key = col.get("color", "")

            raw_value = self._extract(payload, key)

            fmt_fn = _FORMATTERS.get(fmt_key, _fmt_raw)
            text = fmt_fn(raw_value)

            item = QTableWidgetItem(text)
            item.setForeground(QColor(theme.text.primary))

            # Tooltip: prefer the formatted value; fall back to raw
            if text:
                item.setToolTip(text)
            elif raw_value is not None:
                item.setToolTip(str(raw_value))

            if color_key:
                color, bold = _color_for(color_key, raw_value, theme)
                if color is not None:
                    item.setForeground(color)
                if bold:
                    f = QFont()
                    f.setBold(True)
                    item.setFont(f)

            if fmt_key in _RIGHT_ALIGN_FMT:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            self._table.setItem(0, col_idx, item)

        while self._table.rowCount() > self.MAX_ROWS:
            self._table.removeRow(self._table.rowCount() - 1)

    @staticmethod
    def _extract(payload: dict, path: str) -> Any:
        """Support dotted paths for nested fields, e.g. `zone.side`."""
        if not path:
            return None
        if "." not in path:
            return payload.get(path)
        cur: Any = payload
        for part in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur