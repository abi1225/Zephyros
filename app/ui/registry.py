from __future__ import annotations

from PySide6.QtWidgets import QWidget

from app.ui.widgets.liquidity_heatmap import LiquidityHeatmap
from app.ui.widgets.logs import LogsWidget
from app.ui.widgets.orderbook import OrderBookWidget
from app.ui.widgets.positions import PositionsWidget
from app.ui.widgets.signals import SignalsWidget
from app.ui.widgets.tape import TapeWidget

WIDGET_REGISTRY: dict[str, type[QWidget]] = {
    "orderbook": OrderBookWidget,
    "tape": TapeWidget,
    "liquidity_heatmap": LiquidityHeatmap,
    "signals": SignalsWidget,
    "positions": PositionsWidget,
    "logs": LogsWidget,
}