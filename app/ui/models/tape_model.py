from __future__ import annotations

from collections import deque
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

from app.core.config import config


class TapeModel(QAbstractTableModel):
    def __init__(self, widget_id: str = "tape") -> None:
        super().__init__()

        widget_cfg = config.ui.widgets.get(widget_id)
        cfg: dict = widget_cfg.model_dump() if widget_cfg is not None else {}

        self.rows_to_show = int(cfg.get("rows_to_show", 30))
        self.price_dp = int(cfg.get("price_decimals", 2))
        self.qty_dp = int(cfg.get("qty_decimals", 2))
        
        # Read the large trade threshold for the active symbol (default to 5.0 BTC)
        # You can set this in default.yaml under tape.large_trade_threshold.BTC
        thresholds = config.tape.large_trade_threshold or {}
        self.large_threshold = float(thresholds.get("BTC", 5.0))

        self._trades: deque = deque(maxlen=self.rows_to_show)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._trades)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return 3

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return ["Price", "Qty", "Time"][section]
            if role == Qt.TextAlignmentRole:
                return int(Qt.AlignCenter)
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None

        trade = self._trades[index.row()]
        price = trade["price"]
        qty = trade["qty"]
        side = trade["side"]
        ts = trade["ts"]
        c = index.column()

        if role == Qt.DisplayRole:
            if c == 0:
                return f"{price:.{self.price_dp}f}"
            if c == 1:
                return f"{qty:.{self.qty_dp}f}"
            return datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")

        if role == Qt.ForegroundRole and c == 0:
            theme = config.ui.theme
            return QColor(
                theme.semantic.up if side == "BUY" else theme.semantic.down
            )

        if role == Qt.BackgroundRole:
            # ─── SMART TAPE: HIGHLIGHT LARGE TRADES ───
            if qty >= self.large_threshold:
                theme = config.ui.theme
                base = QColor(theme.semantic.up if side == "BUY" else theme.semantic.down)
                base.setAlpha(40) # Subtle glowing background for institutional prints
                return base

        if role == Qt.FontRole:
            if qty >= self.large_threshold:
                f = QFont()
                f.setBold(True)
                return f

        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)

        return None

    def prepend(self, trades: list[dict]) -> None:
        if not trades:
            return
        for t in trades:
            self._trades.appendleft(t)
        self.beginResetModel()
        self.endResetModel()