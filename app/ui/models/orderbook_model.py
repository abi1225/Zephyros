from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from app.core.config import config


class OrderBookModel(QAbstractTableModel):
    """Table model for the L2 order book."""

    def __init__(self, widget_id: str = "orderbook") -> None:
        super().__init__()

        widget_cfg = config.ui.widgets.get(widget_id)
        cfg: dict = widget_cfg.model_dump() if widget_cfg is not None else {}

        self.levels: int = int(cfg.get("levels_to_show", 12))
        self.price_dp: int = int(cfg.get("price_decimals", 2))
        self.qty_dp: int = int(cfg.get("qty_decimals", 4))

        self._bids: list[tuple[float, float]] = []
        self._asks: list[tuple[float, float]] = []
        self._max_qty: float = 1.0

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return self.levels * 2

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return 2

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.DisplayRole,
    ):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return ["Price", "Qty"][section]
            if role == Qt.TextAlignmentRole:
                return int(Qt.AlignCenter)
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None

        r = index.row()
        c = index.column()

        if r < self.levels:
            idx = self.levels - 1 - r
            row = self._asks[idx] if idx < len(self._asks) else None
            side = "ask"
        else:
            idx = r - self.levels
            row = self._bids[idx] if idx < len(self._bids) else None
            side = "bid"

        if row is None:
            return None

        price, qty = row
        theme = config.ui.theme

        if role == Qt.DisplayRole:
            if c == 0:
                return f"{price:.{self.price_dp}f}"
            return f"{qty:.{self.qty_dp}f}"

        if role == Qt.ForegroundRole:
            color = theme.semantic.down if side == "ask" else theme.semantic.up
            return QColor(color)

        if role == Qt.BackgroundRole and c == 1:
            base = QColor(
                theme.semantic.down if side == "ask" else theme.semantic.up
            )
            ratio = qty / self._max_qty if self._max_qty > 0 else 0.0
            ratio = max(0.0, min(1.0, ratio))
            base.setAlpha(int(40 + ratio * 120))
            return base

        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)

        return None

    def update_book(
        self,
        bids: list[tuple[float, float]] | list[list[float]],
        asks: list[tuple[float, float]] | list[list[float]],
    ) -> None:
        self._bids = [(float(p), float(q)) for p, q in bids[: self.levels]]
        self._asks = [(float(p), float(q)) for p, q in asks[: self.levels]]

        all_q = [q for _, q in self._bids] + [q for _, q in self._asks]
        self._max_qty = max(all_q) if all_q else 1.0

        if self.rowCount() == 0:
            return

        top_left = self.index(0, 0)
        bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
        self.dataChanged.emit(
            top_left,
            bottom_right,
            [Qt.DisplayRole, Qt.ForegroundRole, Qt.BackgroundRole],
        )