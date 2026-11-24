from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from app.core.config import config
from app.core.event_bus import bus
from app.core.symbols import registry
from app.modules.strategy.schemas import get_schema, strategy_choices
from app.ui.widgets.custom import ComboBox, LotInput

log = logging.getLogger(__name__)


class Ribbon(QWidget):
    """Single top bar: page title, strategy selector, trading cluster, pills."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Ribbon")
        self.setMinimumHeight(int(config.ui.ribbon.height))

        self._alive = True
        self._running = False
        self._warming = False
        self._current_strategy_id = str(config.strategies.active or "")
        theme = config.ui.theme

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 0, 16, 0)
        root.setSpacing(12)

        # ─── Page title ─────────────────────────────────────
        self._page_title = QLabel("Dashboard")
        self._page_title.setObjectName("RibbonPageTitle")
        root.addWidget(self._page_title)

        sep = QLabel("·")
        sep.setObjectName("RibbonContext")
        root.addWidget(sep)

        self._page_context = QLabel("live market data")
        self._page_context.setObjectName("RibbonContext")
        root.addWidget(self._page_context)

        root.addStretch(1)

        # ─── Trading cluster ────────────────────────────────
        self._trading = QWidget()
        self._trading.setStyleSheet("background: transparent;")
        tl = QHBoxLayout(self._trading)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)

        lbl_style = (
            f"color: {theme.text.muted}; "
            f"font-size: {theme.fonts.size_sm}px; "
            f"font-weight: 600; "
            f"text-transform: uppercase;"
        )

        # ─── Strategy selector ──────────────────────────────
        lbl_strat = QLabel("STRATEGY")
        lbl_strat.setStyleSheet(lbl_style)
        tl.addWidget(lbl_strat)

        self._strategy_combo = ComboBox()
        for sid, slabel in strategy_choices():
            self._strategy_combo.addItem(slabel, sid)
        idx = self._strategy_combo.findData(self._current_strategy_id)
        if idx >= 0:
            self._strategy_combo.setCurrentIndex(idx)
        self._strategy_combo.setMinimumWidth(150)
        self._strategy_combo.setMaximumWidth(220)
        self._strategy_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._strategy_combo.currentIndexChanged.connect(self._on_strategy_combo_changed)
        tl.addWidget(self._strategy_combo)

        tl.addSpacing(8)

        # ─── Timeframe selector ─────────────────────────────
        lbl_tf = QLabel("TIMEFRAME")
        lbl_tf.setStyleSheet(lbl_style)
        tl.addWidget(lbl_tf)

        self._timeframe_combo = ComboBox()
        for tf in ["1m", "3m", "5m", "15m", "1h", "4h"]:
            self._timeframe_combo.addItem(tf, tf)
        idx = self._timeframe_combo.findData(config.strategies.strategy_timeframe)
        if idx >= 0:
            self._timeframe_combo.setCurrentIndex(idx)
        self._timeframe_combo.setMinimumWidth(80)
        self._timeframe_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        # Connect change signal so charts update instantly
        self._timeframe_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        tl.addWidget(self._timeframe_combo)

        tl.addSpacing(8)

        # ─── Symbol selector ────────────────────────────────
        lbl_sym = QLabel("SYMBOL")
        lbl_sym.setStyleSheet(lbl_style)
        tl.addWidget(lbl_sym)

        self._symbol_combo = ComboBox()
        for s in registry.enabled():
            self._symbol_combo.addItem(s.display or s.id, s.id)
        active = registry.get_active()
        if active:
            idx = self._symbol_combo.findData(active)
            if idx >= 0:
                self._symbol_combo.blockSignals(True)
                self._symbol_combo.setCurrentIndex(idx)
                self._symbol_combo.blockSignals(False)
        self._symbol_combo.setMinimumWidth(120)
        self._symbol_combo.setMaximumWidth(180)
        self._symbol_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._symbol_combo.currentIndexChanged.connect(self._on_symbol_changed)
        tl.addWidget(self._symbol_combo)

        tl.addSpacing(8)

        # ─── Lot selector ───────────────────────────────────
        lbl_lot = QLabel("LOT")
        lbl_lot.setStyleSheet(lbl_style)
        tl.addWidget(lbl_lot)

        self._lot_input = LotInput()
        self._lot_input.setRange(0.01, 100.0)
        self._lot_input.setSingleStep(0.01)
        self._lot_input.setDecimals(2)
        self._lot_input.setValue(0.01)
        self._lot_input.setMinimumWidth(90)
        self._lot_input.setMaximumWidth(130)
        self._lot_input.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        tl.addWidget(self._lot_input)

        tl.addSpacing(8)

        # ─── Start/Stop ─────────────────────────────────────
        self._toggle = QPushButton("▶  START")
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.setMinimumHeight(34)
        self._toggle.setMinimumWidth(110)
        self._toggle.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._toggle.clicked.connect(self._on_toggle_clicked)
        tl.addWidget(self._toggle)

        self._reload_btn = QPushButton("⟳")
        self._reload_btn.setObjectName("Ghost")
        self._reload_btn.setCursor(Qt.PointingHandCursor)
        self._reload_btn.setMinimumHeight(34)
        self._reload_btn.setFixedWidth(38)
        self._reload_btn.setToolTip("Reload config")
        self._reload_btn.clicked.connect(self._on_reload_clicked)
        tl.addWidget(self._reload_btn)

        root.addWidget(self._trading)

        root.addSpacing(12)

        # ─── Regime pill ────────────────────────────────────
        self._regime_pill = QLabel("")
        self._regime_pill.setTextFormat(Qt.RichText)
        self._regime_pill.setAlignment(Qt.AlignCenter)
        self._regime_pill.setMinimumHeight(26)
        self._regime_pill.setMinimumWidth(160)
        self._regime_pill.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._regime_pill.setObjectName("RegimePill")
        root.addWidget(self._regime_pill)

        # ─── Signal pill ────────────────────────────────────
        self._signal_pill = QLabel("")
        self._signal_pill.setTextFormat(Qt.RichText)
        self._signal_pill.setAlignment(Qt.AlignCenter)
        self._signal_pill.setMinimumHeight(26)
        self._signal_pill.setMinimumWidth(120)
        self._signal_pill.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._signal_pill.setObjectName("SignalPill")
        root.addWidget(self._signal_pill)

        # ─── Status pill ────────────────────────────────────
        self._status = QLabel("")
        self._status.setTextFormat(Qt.RichText)
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setMinimumHeight(26)
        self._status.setMinimumWidth(90)
        self._status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._status.setObjectName("RibbonStatus")
        root.addWidget(self._status)

        # ─── Subscriptions ──────────────────────────────────
        bus.subscribe(config.protocol.topics["status_update"], self._on_status)
        bus.subscribe(config.protocol.topics["strategy_state"], self._on_strategy_state)
        bus.subscribe(
            config.protocol.topics.get("strategy_changed", "strategy_changed"),
            self._on_strategy_changed,
        )
        bus.subscribe("regime_update", self._on_regime_update)
        bus.subscribe(config.protocol.topics["zones_update"], self._on_zones_update)

        # ─── Initial state ──────────────────────────────────
        self._set_toggle_mode("warming")
        self._apply_status_style(False, False)
        self._apply_regime_style("UNKNOWN", {})
        self._apply_signal_style("IDLE", {})

    # ─── Public accessors ──────────────────────────────────

    def current_symbol(self) -> str:
        return str(self._symbol_combo.currentData() or "")

    def current_volume(self) -> float:
        return float(self._lot_input.value())

    def current_strategy(self) -> str:
        return str(self._strategy_combo.currentData() or "")

    def current_timeframe(self) -> str:
        return str(self._timeframe_combo.currentData() or "1m")

    def set_page(self, page_id: str, title: str, context: str) -> None:
        self._page_title.setText(title)
        self._page_context.setText(context)
        hide_on = set(config.ui.ribbon.hide_trading_on_pages or [])
        self._trading.setVisible(page_id not in hide_on)

    def cleanup(self) -> None:
        self._alive = False
        for topic, handler in (
            (config.protocol.topics["status_update"], self._on_status),
            (config.protocol.topics["strategy_state"], self._on_strategy_state),
            (config.protocol.topics.get("strategy_changed", "strategy_changed"), self._on_strategy_changed),
            ("regime_update", self._on_regime_update),
            (config.protocol.topics["zones_update"], self._on_zones_update),
        ):
            try:
                bus.unsubscribe(topic, handler)
            except Exception:
                pass

    # ─── Selector handlers ──────────────────────────────────

    def _on_timeframe_changed(self, _idx: int) -> None:
        if not self._alive:
            return
        new_tf = str(self._timeframe_combo.currentData() or "1m")
        log.info(f"Ribbon: timeframe changed to {new_tf}")
        asyncio.create_task(
            bus.publish(
                config.protocol.topics.get("ui_timeframe_changed", "ui_timeframe_changed"),
                {"timeframe": new_tf},
            )
        )

    def _on_symbol_changed(self, _idx: int) -> None:
        if not self._alive:
            return

        new_symbol = str(self._symbol_combo.currentData() or "")
        if not new_symbol:
            return
        if new_symbol == registry.get_active():
            return

        log.info(f"Ribbon: symbol changed to {new_symbol}")
        registry.set_active(new_symbol)

        if self._running:
            asyncio.create_task(bus.publish(config.protocol.topics["cmd_stop_bot"], {}))
            QMessageBox.information(
                self,
                "Symbol changed",
                f"Symbol switched to {new_symbol}. Strategy stopped — press START to run on {new_symbol}.",
            )

        asyncio.create_task(
            bus.publish(
                config.protocol.topics.get("ui_symbol_changed", "ui_symbol_changed"),
                {"symbol": new_symbol},
            )
        )

    def _on_strategy_combo_changed(self, _idx: int) -> None:
        if not self._alive:
            return

        new_id = str(self._strategy_combo.currentData() or "")
        if not new_id or new_id == self._current_strategy_id:
            return

        if self._running:
            schema = get_schema(new_id)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Switch strategy")
            box.setText(f"Switch to {schema.get('label', new_id)}?")
            box.setInformativeText(
                "The bot is currently running. Switching strategy will "
                "stop it — you'll need to press START again after the swap."
            )
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
            box.setDefaultButton(QMessageBox.Cancel)
            if box.exec() != QMessageBox.Yes:
                self._revert_strategy_combo()
                return

        self._current_strategy_id = new_id
        asyncio.create_task(
            bus.publish(
                config.protocol.topics.get("cmd_select_strategy", "cmd_select_strategy"),
                {"strategy": new_id},
            )
        )

    def _revert_strategy_combo(self) -> None:
        self._strategy_combo.blockSignals(True)
        try:
            idx = self._strategy_combo.findData(self._current_strategy_id)
            if idx >= 0:
                self._strategy_combo.setCurrentIndex(idx)
        finally:
            self._strategy_combo.blockSignals(False)

    async def _on_strategy_changed(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        new_id = str((payload or {}).get("strategy", "")).strip()
        if not new_id or new_id == self._current_strategy_id:
            return
        self._current_strategy_id = new_id
        idx = self._strategy_combo.findData(new_id)
        if idx >= 0:
            self._strategy_combo.blockSignals(True)
            try:
                self._strategy_combo.setCurrentIndex(idx)
            finally:
                self._strategy_combo.blockSignals(False)

    # ─── Toggle mode ────────────────────────────────────────

    def _set_toggle_mode(self, mode: str) -> None:
        if mode == "start":
            self._toggle.setText("▶  START")
            self._toggle.setEnabled(True)
            self._toggle.setStyleSheet("""
                QPushButton {
                    background-color: #ff9f38;
                    border: 1px solid #e08e20;
                    color: #000000;
                    font-weight: 700;
                    border-radius: 17px;
                    font-size: 12px;
                }
                QPushButton:hover { background-color: #f5a623; }
                QPushButton:pressed { background-color: #e08e20; }
            """)
        elif mode == "stop":
            self._toggle.setText("■  STOP")
            self._toggle.setEnabled(True)
            self._toggle.setStyleSheet("""
                QPushButton {
                    background-color: #ff4d4d;
                    border: 1px solid #cc2626;
                    color: #ffffff;
                    font-weight: 700;
                    border-radius: 17px;
                    font-size: 12px;
                }
                QPushButton:hover { background-color: #e63939; }
                QPushButton:pressed { background-color: #cc2626; }
            """)
        elif mode == "warming":
            self._toggle.setText("◌  WARMING...")
            self._toggle.setEnabled(False)
            self._toggle.setStyleSheet("""
                QPushButton {
                    background-color: #2a2f4c;
                    border: 1px solid #52525b;
                    color: #a1a1aa;
                    font-weight: 700;
                    border-radius: 17px;
                    font-size: 12px;
                }
            """)
        else:
            self._toggle.setText("…")
            self._toggle.setEnabled(False)
            self._toggle.setStyleSheet("""
                QPushButton {
                    background-color: #2a2f4c;
                    border: 1px solid #52525b;
                    color: #71717a;
                    font-weight: 700;
                    border-radius: 17px;
                    font-size: 12px;
                }
            """)

    def _on_toggle_clicked(self) -> None:
        if self._running:
            self._set_toggle_mode("busy")
            asyncio.create_task(
                bus.publish(config.protocol.topics["cmd_stop_bot"], {})
            )
        else:
            symbol_id = self._symbol_combo.currentData() or ""
            volume = float(self._lot_input.value())
            timeframe = self._timeframe_combo.currentData() or "1m"
            self._set_toggle_mode("busy")
            asyncio.create_task(
                bus.publish(
                    config.protocol.topics["cmd_start_bot"],
                    {
                        "action": "start_bot",
                        "values": {
                            "symbol": symbol_id,
                            "volume": volume,
                            "timeframe": timeframe,
                        },
                    },
                )
            )

    def _on_reload_clicked(self) -> None:
        QMessageBox.information(
            self,
            "Reload Config",
            "Restart the app to reload config from disk.",
        )

    # ─── Status pill ────────────────────────────────────────

    async def _on_status(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        connected = bool(payload.get("connected", False))
        mt5_ok = bool(payload.get("mt5_connected", False))
        self._apply_status_style(connected, mt5_ok)

    def _apply_status_style(self, connected: bool, mt5_ok: bool) -> None:
        t = config.ui.theme
        b = t.borders

        if connected and mt5_ok:
            text, color = "LIVE", t.semantic.up
        elif connected:
            text, color = "FEED", t.semantic.warn
        else:
            text, color = "OFFLINE", t.semantic.down

        self._status.setText(
            f"<span style='color:{color}; font-weight:700;'>●</span>"
            f" <span style='color:{color}; font-weight:600;'>{text}</span>"
        )
        self._status.setStyleSheet(f"""
        QLabel#RibbonStatus {{
            background-color: {t.surfaces.l3};
            border: 1px solid {b.normal};
            border-radius: 13px;
            padding: 3px 12px;
            font-size: {t.fonts.size_sm}px;
        }}
        """)

    # ─── Strategy state ─────────────────────────────────────

    async def _on_strategy_state(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return

        warming = bool(payload.get("warming", False))
        running = bool(payload.get("running", False))
        ready = bool(payload.get("ready", False))

        self._warming = warming
        self._running = running

        if warming:
            self._set_toggle_mode("warming")
        elif running:
            self._set_toggle_mode("stop")
        elif ready:
            self._set_toggle_mode("start")
        else:
            self._set_toggle_mode("busy")

        if not running and not warming:
            self._apply_signal_style("IDLE", {})

    # ─── Regime pill ────────────────────────────────────────

    async def _on_regime_update(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        regime = payload.get("regime", "UNKNOWN")
        self._apply_regime_style(regime, payload)

    def _apply_regime_style(self, regime: str, payload: dict) -> None:
        t = config.ui.theme
        b = t.borders
        muted = t.text.muted

        if regime == "STEAM":
            color = t.semantic.up
            bg_tint = "rgba(16,185,129,0.15)"
            label = "STEAM"
        elif regime == "KNIFE":
            color = t.semantic.down
            bg_tint = "rgba(239,68,68,0.15)"
            label = "KNIFE"
        elif regime == "STALL":
            color = t.semantic.warn
            bg_tint = "rgba(245,158,11,0.12)"
            label = "STALL"
        elif regime == "EXHAUST":
            color = t.semantic.warn
            bg_tint = "rgba(245,158,11,0.12)"
            label = "EXHAUST"
        elif regime == "WARMING_UP":
            color = t.text.muted
            bg_tint = "rgba(113,113,122,0.06)"
            label = "WARMING UP"
        else:
            color = t.text.muted
            bg_tint = "rgba(113,113,122,0.06)"
            label = regime if regime != "UNKNOWN" else "—"

        self._regime_pill.setText(
            f"<span style='color:{muted}; font-size:{t.fonts.size_sm}px; "
            f"font-weight:600;'>REGIME</span>"
            f"<span style='color:{muted}; font-weight:400;'>: </span>"
            f"<span style='color:{color}; font-size:{t.fonts.size_sm}px; "
            f"font-weight:700;'>{label}</span>"
        )
        self._regime_pill.setStyleSheet(f"""
        QLabel#RegimePill {{
            background-color: {bg_tint};
            border: 1px solid {b.normal};
            border-radius: 13px;
            padding: 3px 12px;
        }}
        """)

    # ─── Signal pill ────────────────────────────────────────

    async def _on_zones_update(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return

        macro_regime = payload.get("macro_regime")
        if macro_regime:
            self._apply_regime_style(macro_regime, payload)

        running = bool(payload.get("running", False))
        if not running:
            self._apply_signal_style("IDLE", {})
            return
        state = str(payload.get("state", "flat")).upper()
        self._apply_signal_style(state, payload)

    def _apply_signal_style(self, state: str, payload: dict) -> None:
        t = config.ui.theme
        b = t.borders
        muted = t.text.muted

        if state == "LONG":
            color = t.semantic.up
            bg_tint = "rgba(0,245,155,0.15)"
        elif state == "SHORT":
            color = t.semantic.down
            bg_tint = "rgba(255,77,77,0.15)"
        elif state in ("WATCHING", "ARMED", "TEST_PHASE"):
            color = t.semantic.warn
            bg_tint = "rgba(245,166,35,0.12)"
        elif state == "FLAT" or state == "IDLE":
            color = t.text.secondary
            bg_tint = "rgba(161,161,170,0.08)"
        else:
            color = t.text.muted
            bg_tint = "rgba(113,113,122,0.06)"

        self._signal_pill.setText(
            f"<span style='color:{muted}; font-size:{t.fonts.size_sm}px; "
            f"font-weight:600;'>SIGNAL</span>"
            f"<span style='color:{muted}; font-weight:400;'>: </span>"
            f"<span style='color:{color}; font-size:{t.fonts.size_sm}px; "
            f"font-weight:700;'>{state}</span>"
        )
        self._signal_pill.setStyleSheet(f"""
        QLabel#SignalPill {{
            background-color: {bg_tint};
            border: 1px solid {b.normal};
            border-radius: 13px;
            padding: 3px 12px;
        }}
        """)