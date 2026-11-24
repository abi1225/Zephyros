from __future__ import annotations

import asyncio
import datetime as _dt
from datetime import datetime, date as _date

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.config import config
from app.core.database import get_db
from app.core.event_bus import bus
from app.ui.base_widget import Panel


def _account_topic() -> str:
    return config.protocol.topics.get("account_update", "account_update")


class RiskPage(QWidget):
    """Risk state, trade history, recent rejections, account details."""

    def __init__(self) -> None:
        super().__init__()
        self._alive = True
        self.destroyed.connect(self._on_destroyed)

        L = config.ui.layout
        layout = QGridLayout(self)
        layout.setSpacing(L.content_spacing)
        layout.setContentsMargins(L.content_margins, L.content_margins,
                                  L.content_margins, L.content_margins)

        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 2)
        layout.setRowStretch(0, 3)
        layout.setRowStretch(1, 2)

        self._state_widget = _StateAndLimitsWidget()
        layout.addWidget(Panel("Risk State", self._state_widget), 0, 0)

        self._history_widget = _TradeHistoryWidget()
        layout.addWidget(Panel("Trade History", self._history_widget), 0, 1)

        self._rejections_widget = _RejectionsWidget()
        layout.addWidget(Panel("Recent Rejections", self._rejections_widget), 1, 0)

        self._account_widget = _AccountWidget()
        layout.addWidget(Panel("Account", self._account_widget), 1, 1)

        bus.subscribe(config.protocol.topics["risk_state"], self._on_risk_state)
        bus.subscribe(config.protocol.topics["signal_rejected"], self._on_rejected)
        bus.subscribe(config.protocol.topics["trade_closed"], self._on_trade_closed)
        bus.subscribe(_account_topic(), self._on_account)
        bus.subscribe(config.protocol.topics["mt5_status"], self._on_mt5_status)

    def _on_destroyed(self, *_args) -> None:
        self._alive = False

    def cleanup(self) -> None:
        self._alive = False
        for topic, handler in (
            (config.protocol.topics["risk_state"], self._on_risk_state),
            (config.protocol.topics["signal_rejected"], self._on_rejected),
            (config.protocol.topics["trade_closed"], self._on_trade_closed),
            (_account_topic(), self._on_account),
            (config.protocol.topics["mt5_status"], self._on_mt5_status),
        ):
            try:
                bus.unsubscribe(topic, handler)
            except Exception:
                pass

    async def _on_risk_state(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        self._state_widget.update_from(payload)

    async def _on_rejected(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        self._rejections_widget.add(payload)

    async def _on_trade_closed(self, _topic: str, _payload: dict) -> None:
        if not self._alive:
            return
        self._history_widget.refresh()

    async def _on_account(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        self._account_widget.set_connected(True)
        self._account_widget.update_from(payload)

    async def _on_mt5_status(self, _topic: str, payload: dict) -> None:
        if not self._alive:
            return
        connected = bool(payload.get("connected", False))
        if not connected:
            self._account_widget.set_connected(False)


# ─── Sub-widgets ──────────────────────────────────────────────

class _StateAndLimitsWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        t = config.ui.theme
        L = config.ui.layout

        root = QVBoxLayout(self)
        root.setContentsMargins(
            L.panel_padding, L.panel_padding, L.panel_padding, L.panel_padding,
        )
        root.setSpacing(12)

        self._labels: dict[str, QLabel] = {}

        state_grid = QGridLayout()
        state_grid.setVerticalSpacing(8)
        state_grid.setHorizontalSpacing(16)
        state_rows = [
            ("day", "Day"),
            ("daily_pnl", "Daily P&L"),
            ("daily_pnl_pct", "Daily P&L %"),
            ("trades", "Trades"),
            ("win_loss", "Wins / Losses"),
            ("open", "Open Positions"),
            ("status", "Status"),
        ]
        for i, (key, text) in enumerate(state_rows):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {t.text.muted}; background: transparent;")
            val = QLabel("—")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setStyleSheet(
                f"color: {t.text.primary}; "
                f"font-family: '{t.fonts.mono}'; "
                f"background: transparent;"
            )
            state_grid.addWidget(lbl, i, 0)
            state_grid.addWidget(val, i, 1)
            self._labels[key] = val
        root.addLayout(state_grid)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(
            f"background-color: {t.borders.subtle}; border: none;"
        )
        root.addWidget(divider)

        limits_grid = QGridLayout()
        limits_grid.setVerticalSpacing(8)
        limits_grid.setHorizontalSpacing(16)
        limits_rows = [
            ("max_daily_loss", "Max daily loss %"),
            ("max_open", "Max open positions"),
            ("cooldown_exit", "Cooldown after exit (s)"),
            ("cooldown_loss", "Cooldown after loss (s)"),
            ("no_pyramiding", "No pyramiding"),
            ("no_martingale", "No martingale"),
        ]
        for i, (key, text) in enumerate(limits_rows):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {t.text.muted}; background: transparent;")
            val = QLabel("—")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setStyleSheet(
                f"color: {t.text.primary}; "
                f"font-family: '{t.fonts.mono}'; "
                f"background: transparent;"
            )
            limits_grid.addWidget(lbl, i, 0)
            limits_grid.addWidget(val, i, 1)
            self._labels[key] = val
        root.addLayout(limits_grid)

        root.addStretch(1)

        self._labels["max_daily_loss"].setText(
            f"{config.risk.max_daily_loss_pct:.2f}"
        )
        self._labels["max_open"].setText(str(config.risk.max_open_positions))
        self._labels["cooldown_exit"].setText(
            str(config.risk.cooldown_after_exit_sec)
        )
        self._labels["cooldown_loss"].setText(
            str(config.risk.cooldown_after_loss_sec)
        )
        self._labels["no_pyramiding"].setText(
            "yes" if config.risk.no_pyramiding else "no"
        )
        self._labels["no_martingale"].setText(
            "yes" if config.risk.no_martingale else "no"
        )

    def update_from(self, payload: dict) -> None:
        t = config.ui.theme
        self._labels["day"].setText(str(payload.get("day") or "—"))

        pnl = float(payload.get("daily_realized_pnl", 0.0))
        pnl_color = t.semantic.up if pnl >= 0 else t.semantic.down
        self._labels["daily_pnl"].setText(f"{pnl:+.2f}")
        self._labels["daily_pnl"].setStyleSheet(
            f"color: {pnl_color}; "
            f"font-family: '{t.fonts.mono}'; "
            f"background: transparent;"
        )

        starting = float(payload.get("starting_equity", 0.0) or 0.0)
        if starting > 0:
            pct = pnl / starting * 100.0
            self._labels["daily_pnl_pct"].setText(f"{pct:+.2f}%")
            self._labels["daily_pnl_pct"].setStyleSheet(
                f"color: {pnl_color}; "
                f"font-family: '{t.fonts.mono}'; "
                f"background: transparent;"
            )
        else:
            self._labels["daily_pnl_pct"].setText("—")

        self._labels["trades"].setText(str(payload.get("daily_trades", 0)))
        self._labels["win_loss"].setText(
            f"{payload.get('daily_wins', 0)} / {payload.get('daily_losses', 0)}"
        )

        open_count = len(payload.get("open_positions") or {})
        max_open = payload.get("max_open_positions", 0)
        self._labels["open"].setText(f"{open_count} / {max_open}")

        if payload.get("trading_halted"):
            self._labels["status"].setText("HALTED")
            self._labels["status"].setStyleSheet(
                f"color: {t.semantic.down}; font-weight: 600; "
                f"background: transparent;"
            )
            self._labels["status"].setToolTip(str(payload.get("halt_reason", "")))
        else:
            self._labels["status"].setText("OK")
            self._labels["status"].setStyleSheet(
                f"color: {t.semantic.up}; font-weight: 600; "
                f"background: transparent;"
            )
            self._labels["status"].setToolTip("")

        if "max_daily_loss_pct" in payload:
            self._labels["max_daily_loss"].setText(
                f"{float(payload['max_daily_loss_pct']):.2f}"
            )
        if "max_open_positions" in payload:
            self._labels["max_open"].setText(str(payload["max_open_positions"]))
        if "cooldown_exit_sec" in payload:
            self._labels["cooldown_exit"].setText(str(payload["cooldown_exit_sec"]))
        if "cooldown_loss_sec" in payload:
            self._labels["cooldown_loss"].setText(str(payload["cooldown_loss_sec"]))


class _AccountWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        t = config.ui.theme
        L = config.ui.layout

        self._connected: bool = False
        self._labels: dict[str, QLabel] = {}

        layout = QGridLayout(self)
        layout.setContentsMargins(
            L.panel_padding, L.panel_padding, L.panel_padding, L.panel_padding,
        )
        layout.setHorizontalSpacing(24)
        layout.setVerticalSpacing(10)

        identity_rows = [
            ("status", "Status"),
            ("login", "Login"),
            ("server", "Server"),
            ("currency", "Currency"),
            ("leverage", "Leverage"),
        ]
        for i, (key, text) in enumerate(identity_rows):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {t.text.muted}; background: transparent;")
            val = QLabel("—")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setStyleSheet(
                f"color: {t.text.primary}; "
                f"font-family: '{t.fonts.mono}'; "
                f"background: transparent;"
            )
            layout.addWidget(lbl, i, 0)
            layout.addWidget(val, i, 1)
            self._labels[key] = val

        money_rows = [
            ("balance", "Balance"),
            ("equity", "Equity"),
            ("margin", "Margin"),
            ("margin_free", "Free margin"),
            ("margin_level", "Margin level"),
            ("profit", "Floating P&L"),
        ]
        for i, (key, text) in enumerate(money_rows):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {t.text.muted}; background: transparent;")
            val = QLabel("—")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setStyleSheet(
                f"color: {t.text.primary}; "
                f"font-family: '{t.fonts.mono}'; "
                f"background: transparent;"
            )
            layout.addWidget(lbl, i, 2)
            layout.addWidget(val, i, 3)
            self._labels[key] = val

        layout.setColumnStretch(0, 0)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 0)
        layout.setColumnStretch(3, 1)
        layout.setRowStretch(len(money_rows), 1)

        self._apply_disconnected_style()

    def set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        if not connected:
            self._apply_disconnected_style()

    def update_from(self, payload: dict) -> None:
        t = config.ui.theme
        cur = str(payload.get("currency", "") or "").strip()
        suffix = f" {cur}" if cur else ""

        def fmt_money(v) -> str:
            try:
                return f"{float(v):,.2f}{suffix}"
            except (TypeError, ValueError):
                return "—"

        self._labels["status"].setText("CONNECTED")
        self._labels["status"].setStyleSheet(
            f"color: {t.semantic.up}; font-weight: 600; "
            f"background: transparent;"
        )

        login = payload.get("login")
        self._labels["login"].setText(str(login) if login else "—")
        self._labels["server"].setText(str(payload.get("server") or "—"))
        self._labels["currency"].setText(cur or "—")

        lev = payload.get("leverage")
        try:
            lev_i = int(lev) if lev else 0
            self._labels["leverage"].setText(f"1:{lev_i}" if lev_i else "—")
        except (TypeError, ValueError):
            self._labels["leverage"].setText("—")

        self._labels["balance"].setText(fmt_money(payload.get("balance")))
        self._labels["equity"].setText(fmt_money(payload.get("equity")))
        self._labels["margin"].setText(fmt_money(payload.get("margin")))
        self._labels["margin_free"].setText(fmt_money(payload.get("margin_free")))

        try:
            ml = float(payload.get("margin_level") or 0.0)
            if ml <= 0:
                self._labels["margin_level"].setText("—")
            else:
                self._labels["margin_level"].setText(f"{ml:,.2f}%")
        except (TypeError, ValueError):
            self._labels["margin_level"].setText("—")

        try:
            pnl = float(payload.get("profit") or 0.0)
            pnl_color = t.semantic.up if pnl >= 0 else t.semantic.down
            self._labels["profit"].setText(f"{pnl:+,.2f}{suffix}")
            self._labels["profit"].setStyleSheet(
                f"color: {pnl_color}; "
                f"font-family: '{t.fonts.mono}'; "
                f"background: transparent;"
            )
        except (TypeError, ValueError):
            self._labels["profit"].setText("—")

    def _apply_disconnected_style(self) -> None:
        t = config.ui.theme
        self._labels["status"].setText("DISCONNECTED")
        self._labels["status"].setStyleSheet(
            f"color: {t.semantic.down}; font-weight: 600; "
            f"background: transparent;"
        )
        for key, label in self._labels.items():
            if key == "status":
                continue
            label.setText("—")
            label.setStyleSheet(
                f"color: {t.text.muted}; "
                f"font-family: '{t.fonts.mono}'; "
                f"background: transparent;"
            )


class _TradeHistoryWidget(QWidget):
    """Closed-trade history with a date-range filter.
    
    All DB queries run on a background thread to avoid blocking the
    UI. Table population is done in bulk with updates disabled so
    clicking a button never freezes the app.
    """

    HEADERS = ["Time", "Symbol", "Side", "Vol", "Entry", "Exit", "P&L", "Reason"]
    ROW_LIMIT = 500
    _REFRESH_DEBOUNCE_MS = 250

    def __init__(self) -> None:
        super().__init__()
        self._alive = True
        self.destroyed.connect(lambda *_: setattr(self, "_alive", False))
        self._db = get_db()

        # ─── Async state ───
        self._refresh_in_progress: bool = False
        self._pending_refresh: bool = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._kick_refresh)

        t = config.ui.theme
        L = config.ui.layout

        root = QVBoxLayout(self)
        root.setContentsMargins(
            L.panel_padding, L.panel_padding, L.panel_padding, L.panel_padding,
        )
        root.setSpacing(8)

        # ─── Toolbar ───
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        lbl_style = f"color: {t.text.muted}; background: transparent;"

        today = _date.today()
        default_from = today.replace(day=max(1, today.day - 7))

        self._from_edit = QDateEdit()
        self._from_edit.setCalendarPopup(True)
        self._from_edit.setDisplayFormat("yyyy-MM-dd")
        self._from_edit.setDate(QDate(default_from.year, default_from.month, default_from.day))
        self._from_edit.setFixedWidth(120)
        self._from_edit.setFixedHeight(28)
        self._from_edit.dateChanged.connect(self._refresh_from_edits)

        self._to_edit = QDateEdit()
        self._to_edit.setCalendarPopup(True)
        self._to_edit.setDisplayFormat("yyyy-MM-dd")
        self._to_edit.setDate(QDate(today.year, today.month, today.day))
        self._to_edit.setFixedWidth(120)
        self._to_edit.setFixedHeight(28)
        self._to_edit.dateChanged.connect(self._refresh_from_edits)

        lbl_from = QLabel("From")
        lbl_from.setStyleSheet(lbl_style)
        lbl_to = QLabel("To")
        lbl_to.setStyleSheet(lbl_style)

        toolbar.addWidget(lbl_from)
        toolbar.addWidget(self._from_edit)
        toolbar.addWidget(lbl_to)
        toolbar.addWidget(self._to_edit)

        toolbar.addSpacing(8)

        self._buttons: list[QPushButton] = []

        for label, days in (("Today", 0), ("7d", 7), ("30d", 30)):
            btn = QPushButton(label)
            btn.setObjectName("Ghost")
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _=False, d=days: self._set_preset(d))
            toolbar.addWidget(btn)
            self._buttons.append(btn)

        btn_all = QPushButton("All")
        btn_all.setObjectName("Ghost")
        btn_all.setFixedHeight(28)
        btn_all.clicked.connect(self._set_all)
        toolbar.addWidget(btn_all)
        self._buttons.append(btn_all)

        toolbar.addStretch(1)

        btn_refresh = QPushButton("⟳  Refresh")
        btn_refresh.setObjectName("Ghost")
        btn_refresh.setFixedHeight(28)
        btn_refresh.clicked.connect(self._refresh)
        toolbar.addWidget(btn_refresh)
        self._buttons.append(btn_refresh)

        root.addLayout(toolbar)

        # ─── Table ───
        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)
        header.setSectionResizeMode(7, QHeaderView.Stretch)

        self._table.setColumnWidth(0, 130)
        self._table.setColumnWidth(1, 90)
        self._table.setColumnWidth(2, 55)
        self._table.setColumnWidth(3, 55)
        self._table.setColumnWidth(4, 95)
        self._table.setColumnWidth(5, 95)
        self._table.setColumnWidth(6, 90)

        root.addWidget(self._table, 1)

        self._summary = QLabel("—")
        self._summary.setStyleSheet(
            f"color: {t.text.muted}; "
            f"font-family: '{t.fonts.mono}'; "
            f"font-size: {t.fonts.size_sm}px; "
            f"background: transparent;"
        )
        root.addWidget(self._summary)

        # ─── Initial load (deferred so UI paints first) ───
        QTimer.singleShot(100, self._refresh)

    # ─── Preset handlers (non-blocking) ────────────────────

    def _set_preset(self, days: int) -> None:
        today = _date.today()
        if days == 0:
            start = today
        else:
            from datetime import timedelta
            start = today - timedelta(days=days)
        self._from_edit.blockSignals(True)
        self._to_edit.blockSignals(True)
        self._from_edit.setDate(QDate(start.year, start.month, start.day))
        self._to_edit.setDate(QDate(today.year, today.month, today.day))
        self._from_edit.blockSignals(False)
        self._to_edit.blockSignals(False)
        self._refresh()

    def _set_all(self) -> None:
        self._from_edit.blockSignals(True)
        self._to_edit.blockSignals(True)
        self._from_edit.setDate(QDate(2000, 1, 1))
        today = _date.today()
        self._to_edit.setDate(QDate(today.year, today.month, today.day))
        self._from_edit.blockSignals(False)
        self._to_edit.blockSignals(False)
        self._refresh()

    def _refresh_from_edits(self) -> None:
        self._refresh()

    def refresh(self) -> None:
        self._refresh()

    # ─── Debounced async refresh ───────────────────────────

    def _refresh(self) -> None:
        """Public entry point. Debounces rapid clicks, never blocks."""
        if not self._alive:
            return
        self._refresh_timer.stop()
        self._refresh_timer.start(self._REFRESH_DEBOUNCE_MS)

    def _kick_refresh(self) -> None:
        if not self._alive:
            return
        if self._refresh_in_progress:
            # Queue a follow-up run
            self._pending_refresh = True
            return
        try:
            asyncio.create_task(self._refresh_async())
        except RuntimeError:
            pass

    async def _refresh_async(self) -> None:
        self._refresh_in_progress = True
        self._set_loading(True)
        try:
            start_ms = int(self._from_edit.date().startOfDay().toMSecsSinceEpoch())
            end_ms = int(self._to_edit.date().endOfDay().toMSecsSinceEpoch())

            try:
                trades = await asyncio.to_thread(
                    self._db.get_closed_trades_range,
                    start_ms, end_ms, self.ROW_LIMIT,
                )
            except Exception:
                trades = []

            if not self._alive:
                return

            self._populate(trades)
            self._update_summary(trades)
        finally:
            self._refresh_in_progress = False
            self._set_loading(False)
            if self._pending_refresh and self._alive:
                self._pending_refresh = False
                self._kick_refresh()

    def _set_loading(self, loading: bool) -> None:
        for btn in self._buttons:
            btn.setEnabled(not loading)
        if loading:
            self._summary.setText("Loading…")
            self._summary.setStyleSheet(
                f"color: {config.ui.theme.text.secondary}; "
                f"font-family: '{config.ui.theme.fonts.mono}'; "
                f"font-size: {config.ui.theme.fonts.size_sm}px; "
                f"background: transparent;"
            )

    # ─── Bulk populate (fast) ──────────────────────────────

    def _populate(self, trades: list[dict]) -> None:
        t = config.ui.theme

        # Block all repaints during bulk insert
        self._table.setUpdatesEnabled(False)
        try:
            self._table.setRowCount(0)
            self._table.setRowCount(len(trades))

            for row_idx, tr in enumerate(trades):
                exit_ts = tr.get("exit_ts") or tr.get("entry_ts") or 0
                time_str = (
                    datetime.fromtimestamp(exit_ts / 1000).strftime("%m-%d %H:%M:%S")
                    if exit_ts else ""
                )
                side = str(tr.get("side", ""))
                pnl = float(tr.get("pnl") or 0.0)
                entry = tr.get("entry_price")
                exit_p = tr.get("exit_price")
                vol = tr.get("volume")

                cells = [
                    time_str,
                    str(tr.get("symbol", "")),
                    side,
                    f"{vol:.2f}" if isinstance(vol, (int, float)) else "",
                    f"{entry:.2f}" if isinstance(entry, (int, float)) else "",
                    f"{exit_p:.2f}" if isinstance(exit_p, (int, float)) else "",
                    f"{pnl:+.2f}",
                    str(tr.get("exit_reason") or ""),
                ]

                for i, txt in enumerate(cells):
                    item = QTableWidgetItem(txt)
                    if i == 2:
                        item.setForeground(
                            QColor(t.semantic.up if side == "BUY" else t.semantic.down)
                        )
                        f = QFont()
                        f.setBold(True)
                        item.setFont(f)
                    elif i == 6:
                        item.setForeground(
                            QColor(t.semantic.up if pnl >= 0 else t.semantic.down)
                        )
                    elif i in (0, 3, 4, 5):
                        item.setForeground(QColor(t.text.secondary))
                    elif i == 7:
                        item.setForeground(QColor(t.text.muted))
                    self._table.setItem(row_idx, i, item)
        finally:
            self._table.setUpdatesEnabled(True)
            self._table.viewport().update()

    def _update_summary(self, trades: list[dict]) -> None:
        t = config.ui.theme
        if not trades:
            self._summary.setText("no trades in range")
            self._summary.setStyleSheet(
                f"color: {t.text.muted}; "
                f"font-family: '{t.fonts.mono}'; "
                f"font-size: {t.fonts.size_sm}px; "
                f"background: transparent;"
            )
            return

        n = len(trades)
        wins = sum(1 for x in trades if (x.get("pnl") or 0) > 0)
        losses = sum(1 for x in trades if (x.get("pnl") or 0) < 0)
        total_pnl = sum(float(x.get("pnl") or 0.0) for x in trades)
        win_rate = (wins / n * 100.0) if n else 0.0

        pnl_color = t.semantic.up if total_pnl >= 0 else t.semantic.down

        self._summary.setText(
            f"{n} trades  ·  "
            f"W {wins} / L {losses}  ·  "
            f"win rate {win_rate:.0f}%  ·  "
            f"total P&L {total_pnl:+.2f}"
        )
        self._summary.setStyleSheet(
            f"color: {pnl_color}; "
            f"font-family: '{t.fonts.mono}'; "
            f"font-size: {t.fonts.size_sm}px; "
            f"background: transparent;"
        )


class _RejectionsWidget(QTableWidget):
    HEADERS = ["Time", "Symbol", "Action", "Reason"]
    MAX_ROWS = 100

    def __init__(self) -> None:
        super().__init__(0, len(self.HEADERS))
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)

    def add(self, payload: dict) -> None:
        import datetime as _dt
        t = config.ui.theme

        ts = int(payload.get("ts", 0)) or None
        time_str = (
            "—" if ts is None
            else _dt.datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
        )

        self.insertRow(0)
        cells = [
            time_str,
            str(payload.get("symbol", "")),
            str(payload.get("action", "")),
            str(payload.get("reject_reason", "")),
        ]
        for i, txt in enumerate(cells):
            item = QTableWidgetItem(txt)
            if i == 2:
                action = cells[2]
                if action == "BUY":
                    item.setForeground(QColor(t.semantic.up))
                elif action == "SELL":
                    item.setForeground(QColor(t.semantic.down))
                else:
                    item.setForeground(QColor(t.text.muted))
            if i == 3:
                item.setForeground(QColor(t.semantic.warn))
            self.setItem(0, i, item)

        while self.rowCount() > self.MAX_ROWS:
            self.removeRow(self.rowCount() - 1)