from __future__ import annotations

from PySide6.QtWidgets import QHeaderView, QTableView, QVBoxLayout

from app.core.config import config
from app.core.event_bus import bus
from app.ui.base_widget import BaseWidget
from app.ui.models.orderbook_model import OrderBookModel


class OrderBookWidget(BaseWidget):
    widget_id = "orderbook"

    def __init__(self) -> None:
        super().__init__()

        self.model = OrderBookModel(self.widget_id)

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

        bus.subscribe(config.protocol.topics["book_update"], self._on_book)

    async def _on_book(self, _topic: str, payload: dict) -> None:
        book = payload.get("data") or payload.get("book") or payload
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        self.model.update_book(bids, asks)