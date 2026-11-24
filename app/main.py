from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qasync
from PySide6.QtWidgets import QApplication

from app.core.config import config
from app.core.module import BaseModule
from app.core.symbols import registry

log = logging.getLogger("zephyros.main")


# ─── Module wiring ─────────────────────────────────────────

def _build_modules() -> list[BaseModule]:
    from app.modules.feed_binance.feed import BinanceFeed
    from app.modules.feed_binance.klines import KlineFeed
    from app.modules.feed_ctrader.feed import CTraderFeed
    from app.modules.aggregator.book_broadcaster import BookBroadcaster
    from app.modules.aggregator.tape_aggregator import TapeAggregator
    from app.modules.aggregator.status_relay import StatusRelay
    from app.modules.data.recorder import L2Recorder
    from app.modules.data.telemetry_recorder import TelemetryRecorder
    from app.modules.strategy.manager import StrategyManager
    from app.modules.risk.manager import RiskManager
    from app.modules.execution.mt5_executor import MT5Executor

    return [
        BinanceFeed(),
        KlineFeed(),
        CTraderFeed(),
        BookBroadcaster(),
        TapeAggregator(),
        StatusRelay(),
        L2Recorder(),
        TelemetryRecorder(),
        StrategyManager(),
        RiskManager(),
        MT5Executor(),
    ]


async def _start_modules(modules: list[BaseModule]) -> list[BaseModule]:
    started: list[BaseModule] = []
    for mod in modules:
        try:
            log.info("Starting module: %s", mod.name)
            await mod.start()
            started.append(mod)
        except Exception:
            log.exception("Failed to start module: %s", mod.name)
            for prev in reversed(started):
                try:
                    await prev.stop()
                except Exception:
                    log.exception("Error stopping %s", prev.name)
            raise
    return started


async def _stop_modules(modules: list[BaseModule]) -> None:
    for mod in reversed(modules):
        try:
            log.info("Stopping module: %s", mod.name)
            await mod.stop()
        except Exception:
            log.exception("Error stopping %s", mod.name)


# ─── UI + asyncio orchestration ───────────────────────────

async def _run_async(app: QApplication) -> None:
    log.info(
        "Zephyros boot — env=%s symbols=%s",
        config.app.env,
        [s.id for s in config.symbols if s.enabled],
    )
    if not registry.enabled():
        log.error("No enabled symbols — check config/default.yaml")
        return

    # 1. Window first — user sees UI immediately; async loop keeps
    #    Qt responsive while modules spin up in the background.
    from app.ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    # 2. Start backend modules. If MT5 takes 5s to connect, the
    #    window is already on screen and interactive.
    modules = _build_modules()
    started = await _start_modules(modules)

    # 3. Wire shutdown:
    #    user clicks X → closeEvent ignores + emits shutdown_requested
    #    → we stop modules → allow_real_close() → real close → aboutToQuit
    quit_event = asyncio.Event()
    shutdown_started = False

    async def _shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            return
        shutdown_started = True
        log.info("Shutdown requested — stopping modules...")
        window.hide()
        try:
            await asyncio.wait_for(_stop_modules(started), timeout=20.0)
        except asyncio.TimeoutError:
            log.warning("Module shutdown timed out — forcing close")
        window.allow_real_close()
        quit_event.set()

    def _on_shutdown_requested() -> None:
        asyncio.create_task(_shutdown())

    window.shutdown_requested.connect(_on_shutdown_requested)
    app.aboutToQuit.connect(quit_event.set)

    try:
        await quit_event.wait()
    finally:
        if not shutdown_started:
            try:
                await asyncio.wait_for(_stop_modules(started), timeout=20.0)
            except asyncio.TimeoutError:
                pass
        log.info("Zephyros shut down cleanly")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.app.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    app = QApplication(sys.argv)
    app.setApplicationName(config.app.name)
    # Qt convention: don't quit when the last dialog closes, only
    # when the main window closes. MainWindow.closeEvent triggers
    # app.aboutToQuit after allow_real_close(), so this is safe.
    app.setQuitOnLastWindowClosed(True)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    try:
        with loop:
            loop.run_until_complete(_run_async(app))
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — exiting")


if __name__ == "__main__":
    main()