from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView,
    QMenu,
    QMessageBox,
    QTableView,
    QVBoxLayout,
)

from app.core.config import config
from app.core.event_bus import bus
from app.ui.base_widget import BaseWidget
from app.ui.models.positions_model import PositionsModel


class PositionsWidget(BaseWidget):
    widget_id = "positions"

    def __init__(self) -> None:
        super().__init__()

        self.model = PositionsModel()

        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.verticalHeader().setVisible(False)
        self.view.setShowGrid(False)
        self.view.setSelectionMode(QTableView.SingleSelection)
        self.view.setSelectionBehavior(QTableView.SelectRows)
        self.view.setEditTriggers(
            QTableView.DoubleClicked | QTableView.EditKeyPressed
        )
        self.view.setAlternatingRowColors(False)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._show_context_menu)

        header = self.view.horizontalHeader()
        for i in range(self.model.columnCount()):
            header.setSectionResizeMode(i, QHeaderView.Stretch)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(self.view)

        bus.subscribe(config.protocol.topics["position_update"], self._on_position)
        self.model.sltp_edited.connect(self._on_sltp_edited)

    # ─── Bus handlers ───────────────────────────────────────

    async def _on_position(self, _topic: str, payload: dict) -> None:
        symbol = payload.get("symbol")
        position = payload.get("position")
        if symbol:
            self.model.set_positions(symbol, position)

    # ─── Context menu ───────────────────────────────────────

    def _show_context_menu(self, pos) -> None:
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        symbol = self.model.symbol_at(idx.row())
        if not symbol:
            return

        menu = QMenu(self.view)

        act_close = menu.addAction("Close position")
        act_close.triggered.connect(lambda: self._confirm_close(symbol))

        if config.risk.allow_manual_sl_tp:
            menu.addSeparator()
            act_clear = menu.addAction("Clear SL/TP")
            act_clear.triggered.connect(lambda: self._clear_sltp(symbol))

        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _confirm_close(self, symbol: str) -> None:
        row = self.model.row_dict(symbol)
        if row is None:
            return

        pnl = row.get("pnl")
        pnl_text = "—" if pnl is None else f"{float(pnl):+.2f}"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Close position")
        box.setText(
            f"Close {row['side']} position on {symbol} at market?"
        )
        box.setInformativeText(
            f"Volume {row['volume']:.2f}  ·  "
            f"Entry {row['entry']:.2f}  ·  "
            f"P&L {pnl_text}  ·  "
            f"This cannot be undone."
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)

        if box.exec() == QMessageBox.Yes:
            asyncio.create_task(
                bus.publish(
                    config.protocol.topics["cmd_manual_close"],
                    {"symbol": symbol},
                )
            )

    def _clear_sltp(self, symbol: str) -> None:
        asyncio.create_task(
            bus.publish(
                config.protocol.topics["cmd_set_sltp"],
                {"symbol": symbol, "sl": 0.0, "tp": 0.0},
            )
        )

    # ─── SL/TP edits ────────────────────────────────────────

    def _on_sltp_edited(self, symbol: str, column: int, value: float) -> None:
        row = self.model.row_dict(symbol)
        if row is None:
            return
        sl = float(row.get("sl", 0.0) or 0.0)
        tp = float(row.get("tp", 0.0) or 0.0)
        if column == PositionsModel.COL_SL:
            sl = value
        elif column == PositionsModel.COL_TP:
            tp = value
        asyncio.create_task(
            bus.publish(
                config.protocol.topics["cmd_set_sltp"],
                {"symbol": symbol, "sl": sl, "tp": tp},
            )
        )