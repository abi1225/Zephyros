from __future__ import annotations

from collections import deque
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

from app.core.config import config


class SignalsModel(QAbstractTableModel):
    COL_TIME = 0
    COL_ACTION = 1
    COL_SYMBOL = 2
    COL_CONF = 3
    COL_REASON = 4

    HEADERS = ["Time", "Action", "Symbol", "Conf", "Reason"]

    def __init__(self, widget_id: str = "signals") -> None:
        super().__init__()

        widget_cfg = config.ui.widgets.get(widget_id)
        cfg: dict = widget_cfg.model_dump() if widget_cfg is not None else {}
        self.max_rows: int = int(cfg.get("max_rows", 50))

        self._signals: deque = deque(maxlen=self.max_rows)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._signals)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.DisplayRole,
    ):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None

        sig = self._signals[index.row()]
        c = index.column()
        theme = config.ui.theme

        if role == Qt.DisplayRole:
            if c == self.COL_TIME:
                return datetime.fromtimestamp(sig["ts"] / 1000).strftime("%H:%M:%S")
            if c == self.COL_ACTION:
                return sig["action"]
            if c == self.COL_SYMBOL:
                return sig["symbol"]
            if c == self.COL_CONF:
                return f"{sig['confidence'] * 100:.0f}%"
            if c == self.COL_REASON:
                return sig.get("reason", "")

        if role == Qt.ForegroundRole:
            if c == self.COL_ACTION:
                action = sig["action"]
                if action == "BUY":
                    return QColor(theme.semantic.up)
                if action == "SELL":
                    return QColor(theme.semantic.down)
                return QColor(theme.text.muted)
            if c == self.COL_TIME:
                return QColor(theme.text.muted)   # was theme.text_muted
            if c == self.COL_CONF:
                return QColor(theme.semantic.warn)

        if role == Qt.TextAlignmentRole:
            if c in (self.COL_TIME, self.COL_CONF):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if c == self.COL_ACTION:
                return int(Qt.AlignCenter)

        if role == Qt.FontRole and c == self.COL_ACTION:
            f = QFont()
            f.setBold(True)
            return f

        return None

    def prepend(self, signal: dict) -> None:
        self.beginResetModel()
        self._signals.appendleft(signal)
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._signals.clear()
        self.endResetModel()