from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QWidget

from app.core.config import config
from app.ui.base_widget import Panel
from app.ui.registry import WIDGET_REGISTRY


class DashboardPage(QWidget):
    """The main trading dashboard — orderbook, tape, heatmap, signals, positions."""

    WIDGET_IDS = ["orderbook", "tape", "liquidity_heatmap", "signals", "positions"]

    def __init__(self) -> None:
        super().__init__()
        grid = config.ui.grid

        layout = QGridLayout(self)
        layout.setSpacing(grid.gap)
        layout.setContentsMargins(
            grid.padding, grid.padding, grid.padding, grid.padding,
        )

        entries: list[tuple[str, object]] = []
        for wid in self.WIDGET_IDS:
            wcfg = config.ui.widgets.get(wid)
            if wcfg is None or not wcfg.enabled:
                continue
            entries.append((wid, wcfg))

        if not entries:
            return

        min_row = min(wcfg.position.row for _, wcfg in entries)
        min_col = min(wcfg.position.col for _, wcfg in entries)
        max_row = max(
            wcfg.position.row + wcfg.position.row_span for _, wcfg in entries
        )
        max_col = max(
            wcfg.position.col + wcfg.position.col_span for _, wcfg in entries
        )

        rows_used = max(max_row - min_row, 1)
        cols_used = max(max_col - min_col, 1)

        for c in range(cols_used):
            layout.setColumnStretch(c, 1)

        weights = list(grid.row_weights or [])
        if len(weights) >= max_row:
            used_weights = weights[min_row:max_row]
        else:
            used_weights = [1] * rows_used
        for r in range(rows_used):
            w = int(used_weights[r]) if r < len(used_weights) else 1
            layout.setRowStretch(r, max(w, 1))

        for wid, wcfg in entries:
            klass = WIDGET_REGISTRY.get(wid)
            if klass is None:
                continue
            body = klass()
            panel = Panel(wcfg.title, body)
            pos = wcfg.position
            layout.addWidget(
                panel,
                pos.row - min_row,
                pos.col - min_col,
                pos.row_span,
                pos.col_span,
            )