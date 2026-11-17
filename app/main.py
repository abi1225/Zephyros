from __future__ import annotations

import asyncio
import logging
import sys

import qasync
from PySide6.QtWidgets import QApplication

from app.core.config import config
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.modules.aggregator.book_broadcaster import BookBroadcaster
from app.modules.aggregator.status_relay import StatusRelay
from app.modules.aggregator.tape_aggregator import TapeAggregator
from app.modules.data.recorder import L2Recorder
from app.modules.execution.mt5_executor import MT5Executor
from app.modules.execution.trade_journal import TradeJournal
from app.modules.feed_binance.feed import BinanceFeed
from app.modules.feed_binance.klines import KlineFeed
from app.modules.feed_ctrader.feed import CTraderFeed
from app.modules.risk.manager import RiskManager
from app.modules.strategy.manager import StrategyManager
from app.ui.main_window import MainWindow
from app.modules.data.telemetry_recorder import TelemetryRecorder  
from app.modules.test.synthetic_xauusd import SyntheticXAUUSD

logging.basicConfig(
    level=getattr(logging, config.app.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("app.main")
_emit_log = logging.getLogger("app.emit")


async def _forward_log_emit(_topic: str, payload: dict) -> None:
    try:
        level = str(payload.get("level", "info")).lower()
        module = str(payload.get("module", "-"))
        message = str(payload.get("message", ""))
        logger = logging.getLogger(module)
        log_level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warn": logging.WARNING,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }.get(level, logging.INFO)
        logger.log(log_level, message)
    except Exception:
        pass


async def _forward_alert(_topic: str, payload: dict) -> None:
    try:
        level = str(payload.get("level", "warn")).lower()
        module = str(payload.get("module", "-"))
        reason = str(payload.get("reason", ""))
        symbol = payload.get("symbol", "")
        logger = logging.getLogger(f"ALERT.{module}")
        tag = f"[{symbol}] " if symbol else ""
        msg = f"🚨 {tag}{reason}"
        log_method = {
            "info": logger.info,
            "warn": logger.warning,
            "warning": logger.warning,
            "error": logger.error,
            "critical": logger.critical,
        }.get(level, logger.warning)
        log_method(msg)
    except Exception:
        pass


# Cleaned module list with cTrader added
MODULES: list[BaseModule] = [
    SyntheticXAUUSD(),
    BinanceFeed(),
    KlineFeed(),
    CTraderFeed(),          # <--- cTrader Feed added here
    L2Recorder(),
    TelemetryRecorder(),    
    BookBroadcaster(),
    TapeAggregator(),
    StatusRelay(),
    StrategyManager(),
    RiskManager(),
    MT5Executor(),
    TradeJournal(),
]


async def _boot() -> None:
    bus.subscribe(config.protocol.topics["log_emit"], _forward_log_emit)
    bus.subscribe(config.protocol.topics.get("alert", "alert"), _forward_alert)

    for m in MODULES:
        try:
            await m.start()
            log.info("Module started: %s", m.name)
        except Exception:
            log.exception("Failed to start module: %s", m.name)


async def _shutdown() -> None:
    for m in reversed(MODULES):
        try:
            await m.stop()
        except Exception:
            log.exception("Failed to stop module: %s", m.name)


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()

    async def _lifecycle() -> None:
        await _boot()
        stop = asyncio.Event()

        def _on_shutdown_requested() -> None:
            loop.call_soon_threadsafe(stop.set)

        window.shutdown_requested.connect(_on_shutdown_requested)
        await stop.wait()

        log.info("Shutting down modules…")
        try:
            await _shutdown()
        except Exception:
            log.exception("Shutdown failed")
        log.info("Shutdown complete")

        window.allow_real_close()
        app.quit()

    with loop:
        loop.run_until_complete(_lifecycle())

    return 0


if __name__ == "__main__":
    sys.exit(main())