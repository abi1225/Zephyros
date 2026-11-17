from __future__ import annotations

from PySide6.QtWidgets import QHeaderView, QTableView, QVBoxLayout

from app.core.config import config
from app.core.event_bus import bus
from app.ui.base_widget import BaseWidget
from app.ui.models.tape_model import TapeModel


class TapeWidget(BaseWidget):
    widget_id = "tape"

    def __init__(self) -> None:
        super().__init__()

        self.model = TapeModel(self.widget_id)

        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.view.setShowGrid(False)
        self.view.setSelectionMode(QTableView.NoSelection)
        self.view.setEditTriggers(QTableView.NoEditTriggers)
        self.view.setAlternatingRowColors(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(self.view)

        bus.subscribe(config.protocol.topics["tape_append"], self._on_tape)

    async def _on_tape(self, _topic: str, payload: dict) -> None:
        trades = payload.get("data", {}).get("trades") or payload.get("trades", [])
        if trades:
            self.model.prepend(trades)