from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QFont

from app.core.config import config


class PositionsModel(QAbstractTableModel):
    COL_SYMBOL = 0
    COL_SIDE = 1
    COL_VOLUME = 2
    COL_ENTRY = 3
    COL_PNL = 4
    COL_SL = 5
    COL_TP = 6
    COL_TIME = 7

    HEADERS = ["Symbol", "Side", "Vol", "Entry", "P&L", "SL", "TP", "Time"]

    sltp_edited = Signal(str, int, float)  # symbol, column, new value

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []

    # ─── Qt API ─────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._rows)

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

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        base = super().flags(index)
        if index.column() in (self.COL_SL, self.COL_TP):
            if config.risk.allow_manual_sl_tp:
                return base | Qt.ItemIsEditable
            return base & ~Qt.ItemIsEditable
        return base & ~Qt.ItemIsEditable

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        c = index.column()
        theme = config.ui.theme

        if role == Qt.DisplayRole or role == Qt.EditRole:
            if c == self.COL_SYMBOL:
                return row["symbol"]
            if c == self.COL_SIDE:
                return row["side"]
            if c == self.COL_VOLUME:
                return f"{row['volume']:.2f}"
            if c == self.COL_ENTRY:
                return f"{row['entry']:.2f}"
            if c == self.COL_PNL:
                pnl = row.get("pnl")
                if pnl is None:
                    return "—"
                return f"{float(pnl):+.2f}"
            if c == self.COL_SL:
                sl = float(row.get("sl", 0.0) or 0.0)
                return "" if sl == 0.0 else f"{sl:.2f}"
            if c == self.COL_TP:
                tp = float(row.get("tp", 0.0) or 0.0)
                return "" if tp == 0.0 else f"{tp:.2f}"
            if c == self.COL_TIME:
                ts = row.get("opened_at", 0)
                return (
                    datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
                    if ts else ""
                )

        if role == Qt.ForegroundRole:
            if c == self.COL_SIDE:
                return QColor(
                    theme.semantic.up if row["side"] == "BUY"
                    else theme.semantic.down
                )
            if c == self.COL_SYMBOL:
                return QColor(theme.text.primary)
            if c == self.COL_TIME:
                return QColor(theme.text.muted)
            if c == self.COL_PNL:
                pnl = row.get("pnl")
                if pnl is None:
                    return QColor(theme.text.muted)
                pnl = float(pnl)
                return QColor(
                    theme.semantic.up if pnl >= 0 else theme.semantic.down
                )
            if c in (self.COL_SL, self.COL_TP):
                if not config.risk.allow_manual_sl_tp:
                    return QColor(theme.text.disabled)
                return QColor(theme.semantic.warn)

        if role == Qt.TextAlignmentRole:
            if c in (self.COL_VOLUME, self.COL_ENTRY, self.COL_PNL,
                     self.COL_SL, self.COL_TP, self.COL_TIME):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)

        if role == Qt.FontRole:
            if c == self.COL_SIDE:
                f = QFont()
                f.setBold(True)
                return f
            if c == self.COL_PNL:
                f = QFont()
                f.setBold(True)
                return f

        return None

    def setData(  # noqa: N802
        self, index: QModelIndex, value, role: int = Qt.EditRole
    ) -> bool:
        if role != Qt.EditRole:
            return False
        c = index.column()
        if c not in (self.COL_SL, self.COL_TP):
            return False
        if not config.risk.allow_manual_sl_tp:
            return False

        try:
            new_val = float(str(value).strip() or "0")
        except (TypeError, ValueError):
            return False
        if new_val < 0:
            return False

        row = self._rows[index.row()]
        if c == self.COL_SL:
            row["sl"] = new_val
        else:
            row["tp"] = new_val

        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        self.sltp_edited.emit(row["symbol"], c, new_val)
        return True

    # ─── Public API ─────────────────────────────────────────

    def set_positions(self, symbol_id: str, position: dict | None) -> None:
        """Update or remove a position row.

        If the symbol already exists, its row is updated in place
        so the row order doesn't jump on every P&L poll.
        """
        if position is None:
            new_rows = [
                r for r in self._rows if r["symbol"] != symbol_id
            ]
            if len(new_rows) == len(self._rows):
                return  # nothing changed
            self.beginResetModel()
            self._rows = new_rows
            self.endResetModel()
            return

        new_row = {
            "symbol": symbol_id,
            "side": position["side"],
            "volume": position["volume"],
            "entry": position["entry"],
            "opened_at": position.get("opened_at", 0),
            "sl": float(position.get("sl", 0.0) or 0.0),
            "tp": float(position.get("tp", 0.0) or 0.0),
            "pnl": position.get("pnl"),
        }

        existing_idx = next(
            (i for i, r in enumerate(self._rows)
             if r["symbol"] == symbol_id),
            None,
        )

        if existing_idx is not None:
            # Update the row in place; emit dataChanged for it
            self._rows[existing_idx] = new_row
            top_left = self.index(existing_idx, 0)
            bottom_right = self.index(
                existing_idx, self.columnCount() - 1,
            )
            self.dataChanged.emit(
                top_left, bottom_right,
                [Qt.DisplayRole, Qt.ForegroundRole, Qt.FontRole],
            )
        else:
            # New row at top
            self.beginInsertRows(
                QModelIndex(), 0, 0,
            )
            self._rows.insert(0, new_row)
            self.endInsertRows()

    def symbol_at(self, row: int) -> str | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]["symbol"]
        return None

    def row_dict(self, symbol_id: str) -> dict | None:
        for r in self._rows:
            if r["symbol"] == symbol_id:
                return dict(r)
        return None