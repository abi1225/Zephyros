from __future__ import annotations

from PySide6.QtWidgets import QHeaderView, QTableView, QVBoxLayout

from app.core.config import config
from app.core.event_bus import bus
from app.ui.base_widget import BaseWidget
from app.ui.models.depth_model import DepthModel


class DepthWidget(BaseWidget):
    widget_id = "depth"

    def __init__(self) -> None:
        super().__init__()

        self.model = DepthModel(self.widget_id)

        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.view.setShowGrid(False)
        self.view.setSelectionMode(QTableView.NoSelection)
        self.view.setEditTriggers(QTableView.NoEditTriggers)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        bus.subscribe(config.protocol.topics["book_update"], self._on_book)

    async def _on_book(self, _topic: str, payload: dict) -> None:
        book = payload.get("data") or payload.get("book") or payload
        self.model.update_book(book.get("bids", []), book.get("asks", []))