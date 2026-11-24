from __future__ import annotations

from PySide6.QtWidgets import QWidget

from app.ui.pages.chart_page import ChartPage
from app.ui.pages.dashboard_page import DashboardPage
from app.ui.pages.journal_page import JournalPage
from app.ui.pages.risk_page import RiskPage
from app.ui.pages.symbols_page import SymbolsPage
from app.ui.pages.debug_page import DebugPage


PAGE_REGISTRY: dict[str, type[QWidget]] = {
    "dashboard": DashboardPage,
    "chart": ChartPage,
    "journal": JournalPage,
    "risk": RiskPage,
    "symbols": SymbolsPage,
    "debug": DebugPage,
}

PAGE_META: dict[str, dict[str, str]] = {
    "dashboard": {"title": "Dashboard", "context": "live market data"},
    "chart":     {"title": "Chart",     "context": "price action & zones"},
    "journal":   {"title": "Journal",   "context": "trade history & stats"},
    "risk":      {"title": "Risk",      "context": "limits & state"},
    "symbols":   {"title": "Symbols",   "context": "registry & mapping"},
    "debug":     {"title": "Debug",     "context": "bot decision telemetry"},
}