from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QWidget

from app.core.config import config
from app.ui.base_widget import Panel
from app.ui.registry import WIDGET_REGISTRY


class Dashboard(QWidget):
    def __init__(self) -> None:
        super().__init__()
        grid_cfg = config.ui.grid

        layout = QGridLayout(self)
        layout.setSpacing(grid_cfg.gap)
        layout.setContentsMargins(
            grid_cfg.padding,
            grid_cfg.padding,
            grid_cfg.padding,
            grid_cfg.padding,
        )

        # Column stretch: equal by default, so col_span determines width.
        for c in range(grid_cfg.cols):
            layout.setColumnStretch(c, 1)

        # Row stretch: from config if provided, else equal.
        weights = grid_cfg.row_weights or [1] * grid_cfg.rows
        for r, weight in enumerate(weights):
            layout.setRowStretch(r, int(weight))

        for widget_id, wcfg in config.ui.widgets.items():
            if not wcfg.enabled:
                continue
            klass = WIDGET_REGISTRY.get(widget_id)
            if klass is None:
                continue

            body = klass()
            widget = Panel(wcfg.title, body)
            pos = wcfg.position

            layout.addWidget(
                widget,
                pos.row,
                pos.col,
                pos.row_span,
                pos.col_span,
            )

        # Top strip (statusbar + controls) needs enough height for
        # spinboxes and buttons to render without clipping.
        layout.setRowMinimumHeight(0, 34)

        # Logs panel benefits from a minimum as well.
        logs = config.ui.widgets.get("logs")
        if logs is not None and logs.enabled:
            layout.setRowMinimumHeight(logs.position.row, 80)