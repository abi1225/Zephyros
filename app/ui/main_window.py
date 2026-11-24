from __future__ import annotations

import asyncio
import logging
import time
import uuid

from PySide6.QtCore import Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.config import config
from app.core.event_bus import bus
from app.modules.strategy.base import Signal as StrategySignal
from app.ui.pages.registry import PAGE_META, PAGE_REGISTRY
from app.ui.ribbon import Ribbon
from app.ui.sidebar import Sidebar
from app.ui.theme import build_stylesheet
from app.ui.widgets.logs import LogsWidget

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    shutdown_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        w = config.app.window

        self.setWindowTitle(w.title)
        self.setStyleSheet(build_stylesheet())

        screen = QApplication.primaryScreen().availableGeometry()
        target_w = min(w.width, int(screen.width() * 0.9))
        target_h = min(w.height, int(screen.height() * 0.9))

        self.resize(target_w, target_h)
        self.setMinimumSize(w.min_width, w.min_height)
        self.move(
            screen.center().x() - self.width() // 2,
            screen.center().y() - self.height() // 2
        )

        self._allow_real_close = False
        self._page_widgets: dict[str, QWidget] = {}
        self._page_index: dict[str, int] = {}
        self._live_positions: dict[str, dict] = {}
        self._strategy_running = False

        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.item_clicked.connect(self._on_sidebar_item)
        root_layout.addWidget(self.sidebar)

        content = QWidget()
        content.setStyleSheet(
            f"background-color: {config.ui.theme.surfaces.l0};"
        )
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.ribbon = Ribbon()
        content_layout.addWidget(self.ribbon)

        self.stack = QStackedWidget()
        for page_cfg in sorted(
            [p for p in config.ui.sidebar.items], key=lambda p: p.order,
        ):
            if page_cfg.id in ("system", "settings", "about"):
                continue

            klass = PAGE_REGISTRY.get(page_cfg.id)
            if klass is None:
                log.warning("No page registered for id %r", page_cfg.id)
                continue
            widget = klass()
            idx = self.stack.addWidget(widget)
            self._page_widgets[page_cfg.id] = widget
            self._page_index[page_cfg.id] = idx
        content_layout.addWidget(self.stack, 1)

        self.logs_drawer = LogsWidget()
        content_layout.addWidget(self.logs_drawer)

        root_layout.addWidget(content, 1)

        bus.subscribe(
            config.protocol.topics["position_update"], self._on_position
        )
        bus.subscribe(
            config.protocol.topics["strategy_state"], self._on_strategy_state
        )

        if self._page_index:
            first = min(
                self._page_index.keys(),
                key=lambda pid: self._page_index[pid],
            )
            self._select_page(first)

        if w.start_maximized:
            self.showMaximized()

    def allow_real_close(self) -> None:
        self._allow_real_close = True
        self.close()

    def closeEvent(self, event) -> None:
        if not self._allow_real_close:
            event.ignore()
            self.shutdown_requested.emit()
            return

        for topic, handler in (
            (config.protocol.topics["position_update"], self._on_position),
            (config.protocol.topics["strategy_state"], self._on_strategy_state),
        ):
            try:
                bus.unsubscribe(topic, handler)
            except Exception:
                pass

        for target in (self.ribbon, self.logs_drawer, *self._page_widgets.values()):
            cleanup = getattr(target, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    pass
        super().closeEvent(event)

    def _on_sidebar_item(self, item_id: str) -> None:
        log.info(f"Sidebar item clicked: {item_id}")
        if item_id in self._page_index:
            self._select_page(item_id)
            return
        if item_id == "settings":
            self._show_settings_menu()
            return
        if item_id == "about":
            self._show_about()
            return
        log.debug("Unhandled sidebar item: %s", item_id)

    def _select_page(self, page_id: str) -> None:
        idx = self._page_index.get(page_id)
        if idx is None:
            return
        self.stack.setCurrentIndex(idx)
        self.sidebar.set_active(page_id)

        meta = PAGE_META.get(page_id, {})
        self.ribbon.set_page(
            page_id,
            title=meta.get("title", page_id.title()),
            context=meta.get("context", ""),
        )

    def _show_settings_menu(self) -> None:
        log.info("Opening settings menu")
        menu = QMenu(self)
        act_reload = menu.addAction("Reload Config")
        act_reload.triggered.connect(self._reload_config)
        act_log_zones = menu.addAction("Log Zones")
        act_log_zones.triggered.connect(self._log_zones)

        menu.addSeparator()
        act_test = menu.addAction("Run Strategy Pipeline Test")
        act_test.triggered.connect(self._test_trade)

        menu.addSeparator()
        act_about = menu.addAction("About")
        act_about.triggered.connect(self._show_about)
        menu.addSeparator()
        act_quit = menu.addAction("Quit")
        act_quit.triggered.connect(self.close)

        pos = QCursor.pos()
        screen = QApplication.primaryScreen().availableGeometry()
        if pos.x() < screen.left() or pos.x() > screen.right():
            pos.setX(screen.center().x())
        if pos.y() < screen.top() or pos.y() > screen.bottom():
            pos.setY(screen.center().y())

        menu.exec(pos)

    def _reload_config(self) -> None:
        QMessageBox.information(
            self, "Reload Config", "Restart the app to reload config from disk.",
        )

    def _log_zones(self) -> None:
        asyncio.create_task(
            bus.publish(config.protocol.topics["cmd_log_zones"], {})
        )

    def _show_about(self) -> None:
        QMessageBox.information(
            self, "About", f"{config.app.name}\nenv: {config.app.env}",
        )

    async def _on_strategy_state(self, _topic: str, payload: dict) -> None:
        self._strategy_running = bool(payload.get("running", False))

    async def _on_position(self, _topic: str, payload: dict) -> None:
        symbol = payload.get("symbol")
        position = payload.get("position")
        if not symbol:
            return
        if position is None:
            self._live_positions.pop(symbol, None)
        else:
            self._live_positions[symbol] = dict(position)

    # ─── Pipeline Smoke Test ────────────────────────────────

    def _test_trade(self) -> None:
        log.info("_test_trade called")
        symbol = self.ribbon.current_symbol()
        volume = self.ribbon.current_volume()
        strategy = self.ribbon.current_strategy()
        log.info(f"_test_trade: symbol={symbol} volume={volume} strategy={strategy}")

        if not symbol:
            QMessageBox.warning(self, "Test trade", "No symbol selected.")
            return
        if symbol in self._live_positions:
            QMessageBox.warning(
                self, "Test trade",
                f"Already an open position on {symbol}."
            )
            return

        hold_sec = int(config.app.test_trade_close_sec)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Strategy Pipeline Test")
        box.setText(f"Run full strategy pipeline test for {symbol}?")
        box.setInformativeText(
            f"This will PAUSE the live Binance feed, inject synthetic "
            f"market data (Klines + Depth + Tape), and trigger a SPRING "
            f"entry on {strategy}.\n\n"
            f"A real order will be placed on MT5, then closed after "
            f"{hold_sec} seconds.\n\n"
            f"The live feed will be resumed automatically at the end."
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)

        if box.exec() != QMessageBox.Yes:
            log.info("_test_trade: user cancelled")
            return

        log.info("_test_trade: launching _run_pipeline_test")
        asyncio.create_task(
            self._run_pipeline_test(symbol, volume, hold_sec)
        )

    async def _run_pipeline_test(
        self, symbol: str, volume: float, hold_sec: int,
    ) -> None:
        now_ms = int(time.time() * 1000)
        pause_topic = config.protocol.topics.get("cmd_pause_feed", "cmd_pause_feed")
        resume_topic = config.protocol.topics.get("cmd_resume_feed", "cmd_resume_feed")
        clear_risk_topic = config.protocol.topics.get(
            "cmd_clear_risk_state", "cmd_clear_risk_state"
        )

        try:
            # ─── 0. Clear stale RiskManager state ───
            await self._log_line("[TEST] Clearing stale RiskManager state...")
            await bus.publish(clear_risk_topic, {"symbol": symbol})
            await asyncio.sleep(0.2)

            # ─── 1. Pause live feeds ───
            await self._log_line("[TEST] Pausing live Binance feed (test isolation)...")
            await bus.publish(pause_topic, {"symbol": symbol})
            await asyncio.sleep(0.3)

            # ─── 2. Force-stop and restart with override klines ───
            await self._log_line("[TEST] Force-stopping strategy to clear stale state...")
            await bus.publish(config.protocol.topics["cmd_stop_bot"], {})
            await asyncio.sleep(0.5)

            synthetic_klines = []
            for i in range(100):
                t = now_ms - (100 - i) * 60_000
                synthetic_klines.append({
                    "symbol": symbol,
                    "interval": "1m",
                    "kline": {
                        "t": t,
                        "o": 77000.00,
                        "h": 77000.50,
                        "l": 76999.50,
                        "c": 77000.00,
                        "v": 100,
                        "closed": True,
                    },
                })

            await self._log_line("[TEST] Starting strategy with 77,000 warmup klines...")
            await bus.publish(config.protocol.topics["cmd_start_bot"], {
                "action": "start_bot",
                "values": {
                    "symbol": symbol,
                    "volume": volume,
                    "override_klines": synthetic_klines,
                },
            })
            for _ in range(50):
                await asyncio.sleep(0.1)
                if self._strategy_running:
                    break
            if not self._strategy_running:
                await self._log_line("[TEST] Strategy failed to start.", level="error")
                return

            await asyncio.sleep(0.5)

            # ─── 3. Warmup with low-volume baseline ───
            await self._log_line(
                "[TEST] Warmup: injecting 6s of low-volume baseline flow "
                "(qty=0.001 BUY per second)..."
            )
            for _ in range(6):
                await bus.publish(config.protocol.topics["feed_trade"], {
                    "symbol": symbol,
                    "trade": {
                        "ts": int(time.time() * 1000),
                        "price": 77000.00,
                        "qty": 0.001,
                        "side": "BUY",
                    },
                })
                await asyncio.sleep(1.0)

            await self._log_line("[TEST] Warmup done. Median established at ~0.001.")

            # ─── 4. Inject depth ───
            bids = [[77000.00 - i * 0.01, 50.0 if i == 0 else 1.0] for i in range(20)]
            asks = [[77000.00 + (i + 1) * 0.01, 1.0] for i in range(20)]
            asks.append([77006.00, 50.0])
            asks.sort(key=lambda x: x[0])
            await bus.publish(config.protocol.topics["feed_depth"], {
                "symbol": symbol,
                "book": {"bids": bids, "asks": asks},
            })
            await asyncio.sleep(0.3)

            # ─── 5. Phase 1: Aggressive sellers ───
            await self._log_line("[TEST] Phase 1: Aggressive sellers at 77,000 (test)...")
            for _ in range(20):
                await bus.publish(config.protocol.topics["feed_trade"], {
                    "symbol": symbol,
                    "trade": {
                        "ts": int(time.time() * 1000),
                        "price": 77000.00,
                        "qty": 5.0,
                        "side": "SELL",
                    },
                })
                await asyncio.sleep(0.05)
            await asyncio.sleep(3.5)

            # ─── 6. Phase 2: Buyers flip ───
            await self._log_line("[TEST] Phase 2: Buyers flip the tape...")
            for _ in range(25):
                await bus.publish(config.protocol.topics["feed_trade"], {
                    "symbol": symbol,
                    "trade": {
                        "ts": int(time.time() * 1000),
                        "price": 77000.00,
                        "qty": 10.0,
                        "side": "BUY",
                    },
                })
                await asyncio.sleep(0.05)

            await self._log_line("[TEST] Waiting up to 10s for execution...")

            # ─── 7. Wait for position ───
            appeared = False
            for _ in range(100):
                await asyncio.sleep(0.1)
                if symbol in self._live_positions:
                    appeared = True
                    break

            if not appeared:
                await self._log_line(
                    "[TEST] No position detected. Check Event Stream for "
                    "'FOUR_PATTERN X → Y' transitions.",
                    level="warn",
                )
                return

            await self._log_line(f"[TEST] Position opened ✅ — holding {hold_sec}s")
            await asyncio.sleep(hold_sec)

            # ─── 8. Close ───
            await self._log_line(f"[TEST] Closing {symbol}...")
            close_sig = StrategySignal(
                strategy="manual", symbol=symbol, action="CLOSE",
                confidence=1.0, reason="test_pipeline_close",
                price=None, metadata={"exit": "test_close"},
            )
            await bus.publish(config.protocol.topics["signal_proposed"], close_sig.to_dict())

            for _ in range(50):
                await asyncio.sleep(0.1)
                if symbol not in self._live_positions:
                    await self._log_line("[TEST] Position closed ✅")
                    break

        finally:
            await bus.publish(resume_topic, {"symbol": symbol})
            await self._log_line("[TEST] Live Binance feed resumed.")
            await self._log_line("[TEST] Pipeline test complete")

    async def _log_line(self, message: str, level: str = "info") -> None:
        await bus.publish(
            config.protocol.topics["log_emit"],
            {"level": level, "module": "test", "message": message},
        )