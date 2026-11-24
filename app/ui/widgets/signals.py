from __future__ import annotations

from PySide6.QtWidgets import QHeaderView, QTableView, QVBoxLayout

from app.core.config import config
from app.core.event_bus import bus
from app.ui.base_widget import BaseWidget
from app.ui.models.signals_model import SignalsModel


class SignalsWidget(BaseWidget):
    widget_id = "signals"

    def __init__(self) -> None:
        super().__init__()

        self.model = SignalsModel(self.widget_id)

        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.verticalHeader().setVisible(False)
        self.view.setShowGrid(False)
        self.view.setSelectionMode(QTableView.NoSelection)
        self.view.setEditTriggers(QTableView.NoEditTriggers)
        self.view.setAlternatingRowColors(False)

        header = self.view.horizontalHeader()
        header.setSectionResizeMode(SignalsModel.COL_TIME, QHeaderView.Fixed)
        header.setSectionResizeMode(SignalsModel.COL_ACTION, QHeaderView.Fixed)
        header.setSectionResizeMode(SignalsModel.COL_SYMBOL, QHeaderView.Fixed)
        header.setSectionResizeMode(SignalsModel.COL_CONF, QHeaderView.Fixed)
        header.setSectionResizeMode(SignalsModel.COL_REASON, QHeaderView.Stretch)
        self.view.setColumnWidth(SignalsModel.COL_TIME, 80)
        self.view.setColumnWidth(SignalsModel.COL_ACTION, 70)
        self.view.setColumnWidth(SignalsModel.COL_SYMBOL, 100)
        self.view.setColumnWidth(SignalsModel.COL_CONF, 60)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(self.view)

        bus.subscribe(config.protocol.topics["signal_new"], self._on_signal)

    async def _on_signal(self, _topic: str, payload: dict) -> None:
        if isinstance(payload, dict):
            self.model.prepend(payload)