from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.config import SymbolCfg, config
from app.core.event_bus import bus
from app.core.symbols import SymbolRegistry, registry
from app.core.symbols_persist import save_symbols


class SymbolsPage(QWidget):
    """Editable symbol registry — mapping canonical IDs to exchange
    and broker names, plus sizing and precision metadata.
    """

    COLUMNS = [
        ("id",             "ID",        False, 110, "text"),
        ("display",        "Display",   True,  140, "text"),
        ("enabled",        "Enabled",   True,  70,  "bool"),
        ("binance",        "Binance",   True,  120, "text"),
        ("mt5",            "MT5",       True,  120, "text"),
        ("price_decimals", "Price dp",  True,  70,  "int"),
        ("qty_decimals",   "Qty dp",    True,  70,  "int"),
        ("tick_size",      "Tick",      True,  80,  "float"),
        ("min_volume",     "Min vol",   True,  80,  "float"),
        ("max_volume",     "Max vol",   True,  90,  "float"),
        ("volume_step",    "Step",      True,  80,  "float"),
    ]

    def __init__(self) -> None:
        super().__init__()
        L = config.ui.layout

        root = QVBoxLayout(self)
        root.setContentsMargins(L.content_margins, L.content_margins,
                                L.content_margins, L.content_margins)
        root.setSpacing(L.content_spacing)

        # Table
        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels([c[1] for c in self.COLUMNS])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.DoubleClicked)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)

        header = self._table.horizontalHeader()
        for i, (_, _, _, width, _) in enumerate(self.COLUMNS):
            self._table.setColumnWidth(i, width)
            header.setSectionResizeMode(i, QHeaderView.Interactive)
        header.setStretchLastSection(True)

        root.addWidget(self._table, 1)

        # Action row
        row = QHBoxLayout()
        row.setSpacing(8)

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._on_add)
        row.addWidget(add_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._on_delete)
        row.addWidget(delete_btn)

        row.addStretch(1)

        revert_btn = QPushButton("Revert")
        revert_btn.setObjectName("Ghost")
        revert_btn.clicked.connect(self._populate)
        row.addWidget(revert_btn)

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._on_save)
        row.addWidget(save_btn)

        root.addLayout(row)

        self._populate()

    # ─── Data ───────────────────────────────────────────────

    def _populate(self) -> None:
        self._table.setRowCount(0)
        for s in config.symbols:
            self._append_row(s.model_dump())

    def _append_row(self, values: dict[str, Any]) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col_idx, (key, _, editable, _, kind) in enumerate(self.COLUMNS):
            value = values.get(key)
            item = self._make_item(kind, value)
            if not editable:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._table.setItem(row, col_idx, item)

    def _make_item(self, kind: str, value: Any) -> QTableWidgetItem:
        item = QTableWidgetItem()
        if kind == "bool":
            item.setFlags(
                Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
            )
            item.setCheckState(Qt.Checked if bool(value) else Qt.Unchecked)
        elif kind in ("int", "float"):
            item.setData(Qt.DisplayRole, "" if value is None else value)
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        else:
            item.setText("" if value is None else str(value))
        return item

    def _read_item(self, row: int, col_idx: int, kind: str) -> Any:
        item = self._table.item(row, col_idx)
        if item is None:
            return None
        if kind == "bool":
            return item.checkState() == Qt.Checked
        text = item.text().strip()
        if kind == "int":
            if text == "":
                return None
            try:
                return int(float(text))
            except ValueError:
                return None
        if kind == "float":
            if text == "":
                return None
            try:
                return float(text)
            except ValueError:
                return None
        return text if text != "" else None

    def _collect(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for r in range(self._table.rowCount()):
            row: dict[str, Any] = {}
            for c_idx, (key, _, _, _, kind) in enumerate(self.COLUMNS):
                row[key] = self._read_item(r, c_idx, kind)
            rows.append(row)
        return rows

    # ─── Buttons ────────────────────────────────────────────

    def _on_add(self) -> None:
        self._append_row({"id": "NEW_SYMBOL", "enabled": True})

    def _on_delete(self) -> None:
        rows = sorted(
            {i.row() for i in self._table.selectedIndexes()}, reverse=True,
        )
        for r in rows:
            self._table.removeRow(r)

    def _on_save(self) -> None:
        rows = self._collect()
        try:
            symbols = [SymbolCfg.model_validate(r) for r in rows]
        except Exception as exc:
            QMessageBox.critical(self, "Invalid symbol", str(exc))
            return

        probe = SymbolRegistry(symbols)
        errors = probe.validate()
        if errors:
            QMessageBox.critical(self, "Validation failed", "\n".join(errors))
            return

        try:
            save_symbols([s.model_dump() for s in symbols])
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return

        registry.reload(symbols)
        QMessageBox.information(
            self, "Saved", f"{len(symbols)} symbol(s) written.",
        )

    # ─── Cleanup ────────────────────────────────────────────

    def cleanup(self) -> None:
        pass