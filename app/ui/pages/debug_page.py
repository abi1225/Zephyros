from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.core.config import config
from app.ui.base_widget import Panel
from app.ui.widgets.decision_panel import DecisionPanel


class DebugPage(QWidget):
    """Strategy-aware debug telemetry page.

    The DecisionPanel inside rebuilds its own columns on
    `strategy_changed`, so this page only needs to wrap it and
    forward cleanup. Which strategy's rows land here is decided
    upstream (Ribbon selector → StrategyManager → TelemetryRecorder),
    and this page simply displays them.
    """

    def __init__(self) -> None:
        super().__init__()
        L = config.ui.layout

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            L.content_margins, L.content_margins,
            L.content_margins, L.content_margins,
        )
        layout.setSpacing(L.content_spacing)

        self.panel = DecisionPanel()
        layout.addWidget(Panel("Decision Telemetry", self.panel), 1)

    def cleanup(self) -> None:
        self.panel.cleanup()