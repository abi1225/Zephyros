from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.config import config
from app.ui.base_widget import Panel

log = logging.getLogger(__name__)


class JournalPage(QWidget):
    """Reads data/trade_journal.jsonl and shows recent trades + summary stats."""

    HEADERS = [
        "Time", "Symbol", "Side", "Scenario", "BBI",
        "Size×", "Entry", "Exit", "P&L", "R", "Reason",
    ]

    REFRESH_SEC = 5
    MAX_ROWS = 200

    def __init__(self) -> None:
        super().__init__()
        self._alive = True
        self.destroyed.connect(self._on_destroyed)

        cfg = config.trade_journal
        self._path = Path(cfg.path)

        L = config.ui.layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            L.content_margins, L.content_margins,
            L.content_margins, L.content_margins,
        )
        layout.setSpacing(L.content_spacing)

        # ─── Summary strip ───
        self._summary_panel = _SummaryPanel()
        layout.addWidget(self._summary_panel)

        # ─── Trade table ───
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self._refresh_btn = QPushButton("⟳  Refresh")
        self._refresh_btn.setObjectName("Ghost")
        self._refresh_btn.setFixedHeight(28)
        self._refresh_btn.clicked.connect(self._reload)
        toolbar.addWidget(self._refresh_btn)

        self._status_lbl = QLabel("—")
        theme = config.ui.theme
        self._status_lbl.setStyleSheet(
            f"color: {theme.text.muted}; "
            f"font-family: '{theme.fonts.mono}'; "
            f"font-size: {theme.fonts.size_sm}px; "
            f"background: transparent;"
        )
        toolbar.addWidget(self._status_lbl)

        toolbar.addStretch(1)
        table_layout.addLayout(toolbar)

        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)

        header = self._table.horizontalHeader()
        for i in range(len(self.HEADERS) - 1):
            header.setSectionResizeMode(i, QHeaderView.Fixed)
        header.setSectionResizeMode(len(self.HEADERS) - 1, QHeaderView.Stretch)

        widths = [110, 90, 55, 70, 70, 65, 95, 95, 90, 70]
        for i, w in enumerate(widths):
            self._table.setColumnWidth(i, w)

        table_layout.addWidget(self._table, 1)

        table_panel = Panel("Recent Trades", table_container)
        layout.addWidget(table_panel, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_SEC * 1000)
        self._timer.timeout.connect(self._reload)
        self._timer.start()

        self._reload()

    def _on_destroyed(self, *_args) -> None:
        self._alive = False
        try:
            self._timer.stop()
        except Exception:
            pass

    def cleanup(self) -> None:
        self._alive = False
        try:
            self._timer.stop()
        except Exception:
            pass

    def _reload(self) -> None:
        if not self._alive:
            return

        trades = self._load_recent()
        self._populate(trades)
        self._summary_panel.update_from(trades)

        stamp = time.strftime("%H:%M:%S")
        self._status_lbl.setText(
            f"{len(trades)} trade(s) — updated {stamp}"
        )

    def _load_recent(self) -> list[dict]:
        if not self._path.exists():
            return []

        try:
            lines = self._path.read_text(encoding="utf-8").strip().split("\n")
        except Exception as exc:
            log.warning(f"Journal read failed: {exc}")
            return []

        trades: list[dict] = []
        for line in lines[-self.MAX_ROWS:]:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        trades.sort(key=lambda t: t.get("ts", 0), reverse=True)
        return trades

    def _populate(self, trades: list[dict]) -> None:
        theme = config.ui.theme
        mono = theme.fonts.mono
        size_sm = theme.fonts.size_sm

        self._table.setUpdatesEnabled(False)
        try:
            self._table.setRowCount(0)
            self._table.setRowCount(len(trades))

            for row, tr in enumerate(trades):
                ts = int(tr.get("ts", 0))
                time_str = (
                    time.strftime("%m-%d %H:%M:%S", time.localtime(ts / 1000))
                    if ts else ""
                )
                side = str(tr.get("side", ""))
                pnl = float(tr.get("pnl") or 0.0)
                r_mult = tr.get("r_multiple")
                r_str = f"{r_mult:+.2f}R" if r_mult is not None else "—"
                bbi = tr.get("bbi_at_entry")
                bbi_str = f"{bbi:+.2f}" if bbi is not None else "—"
                size_mult = tr.get("size_multiplier")
                size_str = f"{size_mult:.2f}×" if size_mult is not None else "—"
                entry_price = tr.get("entry_price")
                exit_price = tr.get("exit_price")
                scenario = str(tr.get("scenario", "—"))
                exit_reason = str(tr.get("exit_reason", ""))

                cells = [
                    time_str,
                    str(tr.get("symbol", "")),
                    side,
                    scenario,
                    bbi_str,
                    size_str,
                    f"{entry_price:.2f}" if isinstance(entry_price, (int, float)) else "—",
                    f"{exit_price:.2f}" if isinstance(exit_price, (int, float)) else "—",
                    f"{pnl:+.2f}",
                    r_str,
                    exit_reason,
                ]

                for col, txt in enumerate(cells):
                    item = QTableWidgetItem(txt)
                    item.setForeground(QColor(theme.text.primary))

                    if col == 2:
                        item.setForeground(QColor(
                            theme.semantic.up if side == "BUY" else theme.semantic.down
                        ))
                        f = QFont()
                        f.setBold(True)
                        item.setFont(f)
                    elif col == 8:
                        item.setForeground(QColor(
                            theme.semantic.up if pnl >= 0 else theme.semantic.down
                        ))
                        f = QFont()
                        f.setBold(True)
                        item.setFont(f)
                    elif col == 9:
                        if r_mult is not None:
                            item.setForeground(QColor(
                                theme.semantic.up if r_mult >= 0 else theme.semantic.down
                            ))
                    elif col == 4:
                        if bbi is not None:
                            if bbi <= -0.3:
                                item.setForeground(QColor(theme.semantic.down))
                            elif bbi >= 0.3:
                                item.setForeground(QColor(theme.semantic.up))
                            else:
                                item.setForeground(QColor(theme.text.muted))
                    elif col in (0, 3, 10):
                        item.setForeground(QColor(theme.text.secondary))

                    item.setFont(QFont(mono, size_sm))
                    self._table.setItem(row, col, item)
        finally:
            self._table.setUpdatesEnabled(True)
            self._table.viewport().update()


class _SummaryPanel(QWidget):
    """Summary stats strip."""

    def __init__(self) -> None:
        super().__init__()
        theme = config.ui.theme
        L = config.ui.layout

        root = QHBoxLayout(self)
        root.setContentsMargins(
            L.panel_padding, L.panel_padding,
            L.panel_padding, L.panel_padding,
        )
        root.setSpacing(24)

        self._labels: dict[str, QLabel] = {}
        stats = [
            ("total_trades", "Total Trades"),
            ("win_rate", "Win Rate"),
            ("total_pnl", "Total P&L"),
            ("avg_r", "Avg R"),
            ("best_scenario", "Best Scenario"),
            ("worst_scenario", "Worst Scenario"),
        ]

        for key, text in stats:
            cell = QWidget()
            cell.setStyleSheet("background: transparent;")
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)

            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {theme.text.muted}; "
                f"font-size: {theme.fonts.size_sm}px; "
                f"font-weight: 600; "
                f"background: transparent;"
            )
            cell_layout.addWidget(lbl)

            val = QLabel("—")
            val.setStyleSheet(
                f"color: {theme.text.primary}; "
                f"font-family: '{theme.fonts.mono}'; "
                f"font-size: {theme.fonts.size_xl}px; "
                f"font-weight: 600; "
                f"background: transparent;"
            )
            cell_layout.addWidget(val)

            self._labels[key] = val
            root.addWidget(cell)

        root.addStretch(1)

    def update_from(self, trades: list[dict]) -> None:
        theme = config.ui.theme

        if not trades:
            for key in self._labels:
                self._labels[key].setText("—")
            return

        total = len(trades)
        wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in trades if (t.get("pnl") or 0) < 0)
        total_pnl = sum(float(t.get("pnl") or 0.0) for t in trades)
        win_rate = (wins / total * 100.0) if total else 0.0

        r_vals = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
        avg_r = (sum(r_vals) / len(r_vals)) if r_vals else 0.0

        by_scenario: dict[str, float] = {}
        for t in trades:
            sc = str(t.get("scenario", "unknown"))
            by_scenario[sc] = by_scenario.get(sc, 0.0) + float(t.get("pnl") or 0.0)

        best_sc = max(by_scenario.items(), key=lambda x: x[1]) if by_scenario else ("—", 0.0)
        worst_sc = min(by_scenario.items(), key=lambda x: x[1]) if by_scenario else ("—", 0.0)

        self._labels["total_trades"].setText(str(total))

        self._labels["win_rate"].setText(f"{win_rate:.1f}%")
        self._labels["win_rate"].setStyleSheet(
            f"color: {theme.semantic.up if win_rate >= 50 else theme.semantic.down}; "
            f"font-family: '{theme.fonts.mono}'; "
            f"font-size: {theme.fonts.size_xl}px; "
            f"font-weight: 600; "
            f"background: transparent;"
        )

        self._labels["total_pnl"].setText(f"{total_pnl:+.2f}")
        self._labels["total_pnl"].setStyleSheet(
            f"color: {theme.semantic.up if total_pnl >= 0 else theme.semantic.down}; "
            f"font-family: '{theme.fonts.mono}'; "
            f"font-size: {theme.fonts.size_xl}px; "
            f"font-weight: 600; "
            f"background: transparent;"
        )

        self._labels["avg_r"].setText(f"{avg_r:+.2f}R")

        self._labels["best_scenario"].setText(
            f"{best_sc[0]} {best_sc[1]:+.2f}" if by_scenario else "—"
        )
        self._labels["best_scenario"].setStyleSheet(
            f"color: {theme.semantic.up}; "
            f"font-family: '{theme.fonts.mono}'; "
            f"font-size: {theme.fonts.size_lg}px; "
            f"font-weight: 600; "
            f"background: transparent;"
        )

        self._labels["worst_scenario"].setText(
            f"{worst_sc[0]} {worst_sc[1]:+.2f}" if by_scenario else "—"
        )
        self._labels["worst_scenario"].setStyleSheet(
            f"color: {theme.semantic.down}; "
            f"font-family: '{theme.fonts.mono}'; "
            f"font-size: {theme.fonts.size_lg}px; "
            f"font-weight: 600; "
            f"background: transparent;"
        )