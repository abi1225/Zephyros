from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.core.config import config
from app.core.database import get_db
from app.core.event_bus import bus
from app.core.module import BaseModule
from app.core.symbols import registry
from app.modules.strategy.base import Signal

log = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False


_FILLING_CONST = {} if not MT5_AVAILABLE else {
    "FOK": mt5.ORDER_FILLING_FOK,
    "IOC": mt5.ORDER_FILLING_IOC,
    "RETURN": mt5.ORDER_FILLING_RETURN,
}


def _signal_failed_topic() -> str:
    return config.protocol.topics.get("signal_failed", "signal_failed")


def _account_topic() -> str:
    return config.protocol.topics.get("account_update", "account_update")


class MT5Executor(BaseModule):
    name = "execution.mt5"

    def __init__(self) -> None:
        self._connected: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._positions: dict[str, dict[str, Any]] = {}
        self._default_volume: float = 0.01
        self._filling_cache: dict[str, int] = {}
        self._db = get_db()

        self._pnl_task: asyncio.Task | None = None
        self._account_task: asyncio.Task | None = None
        self._basis_task: asyncio.Task | None = None

        self._last_btcusdt_price: float = 0.0
        self._last_basis: float | None = None

        self._closing_tickets: set[int] = set()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mt5",
        )

        if not MT5_AVAILABLE:
            await self._log("MetaTrader5 not installed", level="warn")
            await self._publish_mt5_status(False, "package missing")
            return
        if not config.execution.mt5.enabled:
            await self._log("MT5 executor disabled in config")
            await self._publish_mt5_status(False, "disabled in config")
            return

        ok = await self._run_blocking(self._connect)
        if not ok:
            await self._log("MT5 connection failed", level="error")
            await self._publish_mt5_status(False, "connection failed")
            return

        self._connected = True
        await self._publish_mt5_status(True, "connected")

        await self._validate_symbol_mappings()

        bus.subscribe(config.protocol.topics["signal_approved"], self._on_signal)
        bus.subscribe(config.protocol.topics["cmd_start_bot"], self._on_start_cmd)
        bus.subscribe(config.protocol.topics["cmd_manual_close"], self._on_manual_close)
        bus.subscribe(config.protocol.topics["cmd_set_sltp"], self._on_set_sltp)
        bus.subscribe(
            config.protocol.topics.get("cmd_sync_stop", "cmd_sync_stop"),
            self._on_sync_stop,
        )
        bus.subscribe(config.protocol.topics["feed_trade"], self._on_feed_trade)

        try:
            await self._reconcile()
        except Exception:
            log.exception("Reconciliation failed")

        self._pnl_task = asyncio.create_task(self._unrealized_pnl_loop())
        self._account_task = asyncio.create_task(self._account_publish_loop())
        self._basis_task = asyncio.create_task(self._basis_loop())

        await self._log("MT5 executor online")

    async def stop(self) -> None:
        for task in (self._pnl_task, self._account_task, self._basis_task):
            if task and not task.done():
                task.cancel()

        try:
            bus.unsubscribe(config.protocol.topics["feed_trade"], self._on_feed_trade)
        except Exception:
            pass

        if self._connected and MT5_AVAILABLE:
            await self._run_blocking(mt5.shutdown)
            self._connected = False
            await self._publish_mt5_status(False, "shutdown")

        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=False)
            self._executor = None

    async def _publish_mt5_status(self, connected: bool, reason: str) -> None:
        await bus.publish(
            config.protocol.topics["mt5_status"],
            {"connected": connected, "reason": reason, "ts": int(time.time() * 1000)},
        )

    async def _publish_signal_failed(self, sig: Signal, reason: str) -> None:
        await bus.publish(
            _signal_failed_topic(),
            {
                "id": sig.id, "strategy": sig.strategy, "symbol": sig.symbol,
                "action": sig.action, "reason": reason,
                "ts": int(time.time() * 1000),
            },
        )

    def _connect(self) -> bool:
        cfg = config.execution.mt5
        kwargs: dict[str, Any] = {}
        if cfg.terminal_path:
            kwargs["path"] = cfg.terminal_path

        if not mt5.initialize(**kwargs):
            log.error("mt5.initialize() failed: %s", mt5.last_error())
            return False

        if cfg.login is not None:
            if not mt5.login(cfg.login, password=cfg.password, server=cfg.server):
                log.error("mt5.login() failed: %s", mt5.last_error())
                mt5.shutdown()
                return False

        info = mt5.account_info()
        if info is None:
            log.error("mt5.account_info() returned None")
            mt5.shutdown()
            return False

        log.info(
            "MT5 connected: login=%s server=%s balance=%.2f %s",
            info.login, info.server, info.balance, info.currency,
        )
        return True

    async def _run_blocking(self, fn, *args, timeout: float | None = None,
                            **kwargs):
        """Run a blocking MT5 call off-loop with a hard timeout.

        Every MT5 call shares a single worker thread. Without a timeout a
        hung call (dead terminal, dropped IPC) blocks the PnL poller, the
        account poller and every subsequent order, so the bot silently
        stops managing open positions.
        """
        if self._loop is None or self._executor is None:
            return None
        if timeout is None:
            timeout = max(int(config.execution.mt5.call_timeout_ms), 1000) / 1000.0

        if not kwargs:
            fut = self._loop.run_in_executor(self._executor, fn, *args)
        else:
            def _call():
                return fn(*args, **kwargs)
            fut = self._loop.run_in_executor(self._executor, _call)

        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            log.error(
                "MT5 call %s timed out after %.1fs (worker thread may be stuck)",
                getattr(fn, "__name__", fn), timeout,
            )
            return None

    async def _send_order(self, request: dict, timeout: float | None = None):
        """order_send with the configured order timeout."""
        if timeout is None:
            timeout = max(int(config.execution.mt5.order_timeout_ms), 1000) / 1000.0
        return await self._run_blocking(
            mt5.order_send, timeout=timeout, **request,
        )

    async def _validate_symbol_mappings(self) -> None:
        enabled = registry.enabled()
        missing: list[str] = []
        invisible: list[str] = []
        no_tick: list[str] = []

        for sym in enabled:
            if not sym.mt5:
                continue
            info = await self._run_blocking(mt5.symbol_info, sym.mt5)
            if info is None:
                missing.append(f"{sym.id}->{sym.mt5}")
                continue
            if not info.visible:
                ok = await self._run_blocking(mt5.symbol_select, sym.mt5, True)
                if not ok:
                    invisible.append(f"{sym.id}->{sym.mt5}")
                    continue
            tick = await self._run_blocking(mt5.symbol_info_tick, sym.mt5)
            if tick is None:
                no_tick.append(f"{sym.id}->{sym.mt5}")

        if missing:
            await self._log(
                f"MT5 mapping missing for: {', '.join(missing)} — check config/symbols.local.yaml",
                level="error",
            )
        if invisible:
            await self._log(f"MT5 symbols not selectable: {', '.join(invisible)}", level="error")
        if no_tick:
            await self._log(
                f"MT5 symbols have no tick yet (broker may be closed): {', '.join(no_tick)}",
                level="warn",
            )
        if not (missing or invisible or no_tick):
            await self._log(f"MT5 mapping OK for {len(enabled)} enabled symbol(s)")

    async def _on_feed_trade(self, _topic: str, payload: dict) -> None:
        if payload.get("symbol") != "BTC":
            return
        try:
            self._last_btcusdt_price = float(payload["trade"]["price"])
        except (KeyError, TypeError, ValueError):
            pass

    async def _basis_loop(self) -> None:
        try:
            while True:
                if not self._connected or self._last_btcusdt_price <= 0:
                    await asyncio.sleep(1.0)
                    continue
                sym = registry.get("BTC")
                if sym is None or not sym.mt5:
                    await asyncio.sleep(1.0)
                    continue
                try:
                    tick = await self._run_blocking(mt5.symbol_info_tick, sym.mt5)
                except Exception:
                    await asyncio.sleep(1.0)
                    continue
                if tick is None:
                    await asyncio.sleep(1.0)
                    continue
                btc_usd = (tick.bid + tick.ask) / 2.0
                basis = btc_usd / self._last_btcusdt_price

                if self._last_basis is not None:
                    drift = abs(basis - self._last_basis)
                    if drift > 0.005:
                        log.warning(
                            "BTC basis drift: %.4f → %.4f (%.2f%%)",
                            self._last_basis, basis, drift * 100,
                        )
                        await self._log(
                            f"BTC basis drift: {(basis - 1) * 100:+.3f}% "
                            f"(was {(self._last_basis - 1) * 100:+.3f}%)",
                            level="warn",
                        )

                self._last_basis = basis
                await bus.publish("basis_update", {
                    "symbol": "BTC", "btc_usd": btc_usd,
                    "btc_usdt": self._last_btcusdt_price,
                    "basis": basis, "basis_pct": (basis - 1) * 100.0,
                    "ts": int(time.time() * 1000),
                })
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return

    async def _account_publish_loop(self) -> None:
        try:
            while True:
                if not self._connected:
                    await asyncio.sleep(1.0)
                    continue
                try:
                    info = await self._run_blocking(mt5.account_info)
                except Exception:
                    log.exception("account_info poll failed")
                    info = None

                if info is not None:
                    await bus.publish(_account_topic(), {
                        "login": int(getattr(info, "login", 0) or 0),
                        "server": str(getattr(info, "server", "") or ""),
                        "currency": str(getattr(info, "currency", "") or ""),
                        "leverage": int(getattr(info, "leverage", 0) or 0),
                        "balance": float(getattr(info, "balance", 0.0) or 0.0),
                        "equity": float(getattr(info, "equity", 0.0) or 0.0),
                        "margin": float(getattr(info, "margin", 0.0) or 0.0),
                        "margin_free": float(getattr(info, "margin_free", 0.0) or 0.0),
                        "margin_level": float(getattr(info, "margin_level", 0.0) or 0.0),
                        "profit": float(getattr(info, "profit", 0.0) or 0.0),
                        "ts": int(time.time() * 1000),
                    })
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return

    async def _unrealized_pnl_loop(self) -> None:
        try:
            while True:
                if not self._connected or not self._positions:
                    await asyncio.sleep(1.0)
                    continue
                try:
                    broker_positions = await self._run_blocking(mt5.positions_get) or []
                except Exception:
                    log.exception("positions_get failed in pnl poll")
                    await asyncio.sleep(1.0)
                    continue

                seen: set[str] = set()
                for p in broker_positions:
                    sym = registry.by_mt5(p.symbol)
                    if sym is None or sym.id not in self._positions:
                        continue
                    seen.add(sym.id)
                    pos = self._positions[sym.id]
                    pos["pnl"] = float(getattr(p, "profit", 0.0) or 0.0)
                    pos["swap"] = float(getattr(p, "swap", 0.0) or 0.0)
                    pos["commission"] = float(getattr(p, "commission", 0.0) or 0.0)
                    pos["current_price"] = float(getattr(p, "price_current", 0.0) or 0.0)
                    await bus.publish(
                        config.protocol.topics["position_update"],
                        {
                            "symbol": sym.id, "position": dict(pos),
                            "ts": int(time.time() * 1000),
                            "source": "pnl_poll",
                        },
                    )

                stale = [
                    sid for sid in self._positions
                    if sid not in seen
                    and int(self._positions[sid]["ticket"]) not in self._closing_tickets
                ]
                for sid in stale:
                    await self._handle_vanished_position(sid)

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    def _lookup_entry_signal_id(self, ticket: int) -> str:
        try:
            row = self._db.get_trade_by_ticket(int(ticket))
            if row and row.get("signal_id"):
                return str(row["signal_id"])
        except Exception as exc:
            log.debug("Entry signal_id lookup failed for ticket %s: %s", ticket, exc)
        return ""

    def _lookup_entry_plan(self, ticket: int) -> dict:
        """Reconstruct signal_metadata for a closed trade from DB entry_plan."""
        try:
            row = self._db.get_trade_by_ticket(int(ticket))
            if row and row.get("entry_plan"):
                return {"entry_plan": json.loads(row["entry_plan"])}
        except Exception as exc:
            log.debug("Entry plan lookup failed for ticket %s: %s", ticket, exc)
        return {}

    async def _handle_vanished_position(self, symbol_id: str) -> None:
        pos = self._positions.pop(symbol_id, None)
        if pos is None:
            return

        ticket = int(pos["ticket"])
        log.warning(
            "Position %s ticket=%s vanished at broker — likely manually closed",
            symbol_id, ticket,
        )

        pnl = 0.0
        exit_price: float | None = None
        exit_ts = int(time.time() * 1000)

        try:
            deals = await self._run_blocking(mt5.history_deals_get, position=ticket)
            if deals:
                for d in deals:
                    pnl += float(getattr(d, "profit", 0.0) or 0.0)
                    pnl += float(getattr(d, "swap", 0.0) or 0.0)
                    pnl += float(getattr(d, "commission", 0.0) or 0.0)
                    if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT:
                        exit_price = float(d.price)
                        exit_ts = int(d.time * 1000)
        except Exception:
            log.exception("Failed to fetch deals for vanished %s", symbol_id)

        entry_signal_id = self._lookup_entry_signal_id(ticket)
        signal_metadata = self._lookup_entry_plan(ticket)

        await bus.publish(
            config.protocol.topics["position_update"],
            {
                "symbol": symbol_id, "position": None,
                "ts": int(time.time() * 1000),
                "source": "pnl_poll_vanished",
            },
        )
        await bus.publish(
            config.protocol.topics["trade_closed"],
            {
                "symbol": symbol_id, "ticket": ticket,
                "side": pos["side"], "volume": float(pos["volume"]),
                "entry_price": float(pos["entry"]),
                "exit_price": exit_price, "pnl": pnl, "pnl_pct": 0.0,
                "exit_reason": "manual_broker_close",
                "signal_id": entry_signal_id,
                "exit_order_id": None, "ts": exit_ts,
                "signal_metadata": signal_metadata,
            },
        )

        await self._log(
            f"Broker closed {symbol_id} ticket={ticket} pnl={pnl:+.2f} (Manual close)"
        )

    async def _reconcile(self) -> None:
        magic = config.execution.mt5.magic
        broker_positions = await self._run_blocking(mt5.positions_get) or []
        ours = [p for p in broker_positions if int(getattr(p, "magic", 0)) == magic]
        broker_map: dict[int, Any] = {int(p.ticket): p for p in ours}

        for p in ours:
            sym = registry.by_mt5(p.symbol)
            if sym is None:
                log.warning(
                    "Reconcile: MT5 position %s on %s has no canonical mapping",
                    p.ticket, p.symbol,
                )
                continue
            side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
            self._positions[sym.id] = {
                "ticket": int(p.ticket), "side": side,
                "volume": float(p.volume), "entry": float(p.price_open),
                "opened_at": int(p.time * 1000),
                "sl": float(getattr(p, "sl", 0.0) or 0.0),
                "tp": float(getattr(p, "tp", 0.0) or 0.0),
                "pnl": float(getattr(p, "profit", 0.0) or 0.0),
                "swap": float(getattr(p, "swap", 0.0) or 0.0),
                "commission": float(getattr(p, "commission", 0.0) or 0.0),
                "current_price": float(getattr(p, "price_current", 0.0) or 0.0),
            }
            await bus.publish(
                config.protocol.topics["position_update"],
                {
                    "symbol": sym.id,
                    "position": dict(self._positions[sym.id]),
                    "ts": int(time.time() * 1000),
                    "source": "reconcile",
                },
            )
            existing = self._db.get_trade_by_ticket(int(p.ticket))
            if existing is None:
                try:
                    self._db.open_trade(
                        signal_id="", entry_order_id=int(p.ticket),
                        symbol=sym.id, broker_symbol=p.symbol, side=side,
                        volume=float(p.volume), entry_price=float(p.price_open),
                        entry_ts=int(p.time * 1000), strategy="reconciled",
                        status="orphaned",
                        notes="Recovered from MT5 on startup",
                    )
                except Exception as exc:
                    log.warning("Reconcile: DB insert failed: %s", exc)

        for row in self._db.get_open_trades():
            ticket = row.get("entry_order_id")
            if ticket is None or int(ticket) in broker_map:
                continue
            try:
                deals = await self._run_blocking(mt5.history_deals_get, position=int(ticket))
            except Exception:
                deals = None
            if deals:
                pnl = 0.0
                exit_price = None
                exit_ts = None
                for d in deals:
                    pnl += float(getattr(d, "profit", 0.0) or 0.0)
                    pnl += float(getattr(d, "swap", 0.0) or 0.0)
                    pnl += float(getattr(d, "commission", 0.0) or 0.0)
                    if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT:
                        exit_price = float(d.price)
                        exit_ts = int(d.time * 1000)
                try:
                    self._db.close_trade(
                        entry_order_id=int(ticket), exit_order_id=None,
                        exit_price=exit_price,
                        exit_ts=exit_ts or int(time.time() * 1000),
                        pnl=pnl, pnl_pct=None,
                        exit_reason="reconciled_from_history",
                    )
                    log.info("Reconciled trade %s from history: pnl=%+.2f", ticket, pnl)
                except Exception as exc:
                    log.warning("Reconcile close failed: %s", exc)
            else:
                self._db.mark_lost(
                    int(ticket),
                    "No MT5 position and no history deals found",
                )
                log.warning("Trade %s marked lost (no broker trace)", ticket)

        if ours:
            await self._log(f"Reconciled {len(ours)} open position(s) from MT5")

    async def _on_start_cmd(self, _topic: str, payload: dict) -> None:
        values = (payload or {}).get("values", {}) or {}
        vol = values.get("volume")
        if vol is not None:
            try:
                self._default_volume = float(vol)
            except (TypeError, ValueError):
                pass

    async def _on_manual_close(self, _topic: str, payload: dict) -> None:
        if not self._connected:
            return
        if not config.risk.allow_manual_close:
            await self._log("Manual close disabled in config", level="warn")
            return
        symbol = str((payload or {}).get("symbol", ""))
        if not symbol:
            return
        pos = self._positions.get(symbol)
        if pos is None:
            await self._log(f"Manual close: no position on {symbol}", level="warn")
            return
        sym = registry.get(symbol)
        if sym is None or not sym.mt5:
            return
        sig = Signal(
            strategy="manual", symbol=symbol, action="CLOSE",
            confidence=1.0, reason="manual close from UI",
            metadata={"exit": "manual"},
        )
        await self._handle_close(sig, sym.mt5)

    async def _on_set_sltp(self, _topic: str, payload: dict) -> None:
        if not self._connected:
            return
        if not config.risk.allow_manual_sl_tp:
            await self._log("Manual SL/TP disabled in config", level="warn")
            return
        symbol = str((payload or {}).get("symbol", ""))
        try:
            sl = float(payload.get("sl") or 0.0)
            tp = float(payload.get("tp") or 0.0)
        except (TypeError, ValueError):
            return
        if sl < 0 or tp < 0:
            return
        pos = self._positions.get(symbol)
        if pos is None:
            await self._log(f"SL/TP: no position on {symbol}", level="warn")
            return
        sym = registry.get(symbol)
        if sym is None or not sym.mt5:
            return
        request = {
            "action": int(mt5.TRADE_ACTION_SLTP),
            "symbol": str(sym.mt5),
            "position": int(pos["ticket"]),
            "sl": float(sl), "tp": float(tp),
        }
        result = await self._run_blocking(mt5.order_send, **request)
        if result is None:
            err = await self._run_blocking(mt5.last_error)
            await self._log(f"SL/TP order_send returned None: {err}", level="error")
            return
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            await self._log(
                f"SL/TP rejected: retcode={result.retcode} comment={result.comment}",
                level="error",
            )
            return
        pos["sl"] = sl
        pos["tp"] = tp
        await self._log(f"Updated SL/TP for {symbol}: sl={sl:.2f} tp={tp:.2f}")
        await bus.publish(
            config.protocol.topics["position_update"],
            {
                "symbol": symbol, "position": dict(pos),
                "ts": int(time.time() * 1000), "source": "sltp",
            },
        )

    async def _on_sync_stop(self, _topic: str, payload: dict) -> None:
        """Strategy-driven stop update (break-even / trailing)."""
        if not self._connected:
            return
        symbol = str((payload or {}).get("symbol", ""))
        if not symbol:
            return
        try:
            new_stop = float(payload.get("stop") or 0.0)
        except (TypeError, ValueError):
            return
        reason = str(payload.get("reason") or "trail")
        await self.sync_stop(symbol, new_stop, reason)

    async def _on_signal(self, _topic: str, signal_dict: dict) -> None:
        log.info(
            f"[PIPELINE 3→4] Executor received: "
            f"{signal_dict.get('action')} {signal_dict.get('symbol')} "
            f"connected={self._connected}"
        )
        if not self._connected:
            log.warning("[PIPELINE 4] Executor NOT connected — dropping signal")
            try:
                sig = Signal(**{
                    k: signal_dict[k]
                    for k in (
                        "strategy", "symbol", "action", "confidence",
                        "reason", "price", "metadata", "id", "ts",
                    )
                    if k in signal_dict
                })
                await self._publish_signal_failed(sig, "executor not connected")
            except Exception:
                pass
            return
        if signal_dict.get("reject_reason"):
            log.warning(
                f"[PIPELINE 4] Signal has reject_reason: {signal_dict.get('reject_reason')}"
            )
            return
        try:
            sig = Signal(**{
                k: signal_dict[k]
                for k in (
                    "strategy", "symbol", "action", "confidence",
                    "reason", "price", "metadata", "id", "ts",
                )
                if k in signal_dict
            })
        except Exception as exc:
            log.warning("Malformed signal payload: %s", exc)
            return
        sym = registry.get(sig.symbol)
        if sym is None or sym.mt5 is None:
            await self._log(f"No MT5 mapping for {sig.symbol}", level="warn")
            await self._publish_signal_failed(sig, "no MT5 mapping")
            return
        broker_symbol = sym.mt5
        visible = await self._ensure_symbol_visible(broker_symbol)
        if not visible:
            await self._log(f"Symbol {broker_symbol} not available", level="error")
            await self._publish_signal_failed(sig, f"symbol {broker_symbol} not available")
            return
        if sig.action == "CLOSE":
            await self._handle_close(sig, broker_symbol)
        elif sig.action in ("BUY", "SELL"):
            await self._handle_open(sig, broker_symbol)

    async def _ensure_symbol_visible(self, broker_symbol: str) -> bool:
        info = await self._run_blocking(mt5.symbol_info, broker_symbol)
        if info is None:
            return False
        if info.visible:
            return True
        return await self._run_blocking(mt5.symbol_select, broker_symbol, True)

    async def _detect_filling(self, broker_symbol: str) -> int:
        if broker_symbol in self._filling_cache:
            return self._filling_cache[broker_symbol]
        info = await self._run_blocking(mt5.symbol_info, broker_symbol)
        allowed_mask = int(getattr(info, "filling_mode", 1)) if info else 1
        preferred = config.execution.mt5.filling_mode
        order = [preferred]
        for name in ("IOC", "FOK", "RETURN"):
            if name not in order:
                order.append(name)
        # MQL5 SYMBOL_FILLING_* constants: FOK=1, IOC=2, BOC=4, RETURN=8.
        # RETURN is bit 8, not 4 — matching bit 4 flagged RETURN as
        # supported on brokers that actually only allow BOC.
        bit_for = {"FOK": 1, "IOC": 2, "RETURN": 8}
        chosen_name = "IOC"
        for name in order:
            if allowed_mask & bit_for.get(name, 0):
                chosen_name = name
                break
        chosen = int(_FILLING_CONST[chosen_name])
        self._filling_cache[broker_symbol] = chosen
        return chosen

    async def _resolve_position_ticket(
        self, result: Any, broker_symbol: str, magic: int,
    ) -> tuple[int | None, float | None]:
        """Resolve the real POSITION ticket after a market order fills.

        `result.order` is the order ticket, not the position ticket. Using
        it as the position id makes close requests fail and makes the PnL
        poller think the position vanished immediately.

        Strategy: read the order's deals -> take the entry deal's
        position_id -> confirm against positions_get(ticket=...).
        """
        order_ticket = int(getattr(result, "order", 0) or 0)
        deal_ticket = int(getattr(result, "deal", 0) or 0)
        fill_price = getattr(result, "price", None)

        position_id: int | None = None
        try:
            deals = await self._run_blocking(
                mt5.history_deals_get, ticket=deal_ticket,
            ) or []
            for d in deals:
                pid = int(getattr(d, "position_id", 0) or 0)
                if pid > 0:
                    position_id = pid
                    if not fill_price:
                        fill_price = float(getattr(d, "price", 0.0) or 0.0)
                    break
        except Exception:
            log.exception("Failed to resolve position id from deal %s", deal_ticket)
        fallback_price = fill_price

        # Verify against the live position list; this also covers brokers
        # that do not populate position_id on the entry deal.
        try:
            positions = await self._run_blocking(mt5.positions_get) or []
        except Exception:
            positions = []

        for p in positions:
            p_magic = int(getattr(p, "magic", 0) or 0)
            if int(getattr(p, "ticket", 0) or 0) == order_ticket:
                return int(p.ticket), fallback_price
            if position_id is not None and int(p.ticket) == position_id:
                if fallback_price is None:
                    fallback_price = float(getattr(p, "price_open", 0.0) or 0.0)
                return int(p.ticket), fallback_price

        # No match by id: pick the newest position on this symbol with our
        # magic, rather than guessing from the order ticket.
        candidates = [
            p for p in positions
            if str(getattr(p, "symbol", "")) == broker_symbol
            and int(getattr(p, "magic", 0) or 0) == magic
        ]
        if candidates:
            newest = max(candidates, key=lambda p: int(getattr(p, "time", 0)))
            if fallback_price is None:
                fallback_price = float(getattr(newest, "price_open", 0.0) or 0.0)
            log.warning(
                "Position id unresolved from deal %s — matched newest %s "
                "position ticket=%s on %s",
                deal_ticket, broker_symbol, newest.ticket, broker_symbol,
            )
            return int(newest.ticket), fallback_price

        return None, fallback_price

    def _compute_volume(self, sig: Signal) -> float:
        metadata = sig.metadata or {}
        sym_cfg = registry.get(sig.symbol)

        volume_step = (
            float(sym_cfg.volume_step)
            if sym_cfg and sym_cfg.volume_step else 0.01
        )
        min_vol = (
            float(sym_cfg.min_volume)
            if sym_cfg and sym_cfg.min_volume else 0.01
        )
        max_vol = (
            float(sym_cfg.max_volume)
            if sym_cfg and sym_cfg.max_volume else 100.0
        )

        def _quantize(raw: float) -> float:
            steps = math.floor(raw / volume_step + 0.5)
            v = steps * volume_step
            v = max(v, min_vol)
            v = min(v, max_vol)
            return round(v, 4)

        override = metadata.get("volume_override")
        if override is not None:
            try:
                raw = float(override)
            except (TypeError, ValueError):
                raw = 0.0
            if raw > 0:
                return _quantize(raw)

        size_mult = float(metadata.get("size_multiplier", 1.0))
        raw_vol = self._default_volume * size_mult
        return _quantize(raw_vol)

    async def _handle_open(self, sig: Signal, broker_symbol: str) -> None:
        tick = await self._run_blocking(mt5.symbol_info_tick, broker_symbol)
        if tick is None:
            await self._log(f"No tick for {broker_symbol}", level="error")
            await self._publish_signal_failed(sig, "no tick")
            return

        order_type = int(mt5.ORDER_TYPE_BUY if sig.action == "BUY" else mt5.ORDER_TYPE_SELL)
        price = float(tick.ask if sig.action == "BUY" else tick.bid)
        filling = int(await self._detect_filling(broker_symbol))
        volume = self._compute_volume(sig)

        metadata = sig.metadata or {}
        scenario = str(metadata.get("scenario", "n/a"))
        size_mult = float(metadata.get("size_multiplier", 1.0))
        vol_source = (
            "strategy" if metadata.get("volume_override") is not None
            else f"fallback ×{size_mult:.2f}"
        )

        entry_plan = metadata.get("entry_plan", {}) or {}
        stop_dist = float(entry_plan.get("stop_distance", 0.0))

        mt5_stop = 0.0
        if stop_dist > 0:
            mt5_stop = price - stop_dist if sig.action == "BUY" else price + stop_dist
            info = await self._run_blocking(mt5.symbol_info, broker_symbol)
            if info is not None:
                point = float(getattr(info, "point", 0.01) or 0.01)
                min_stop_dist = float(getattr(info, "trade_stops_level", 0) or 0) * point
                if min_stop_dist > 0:
                    if sig.action == "BUY":
                        if price - mt5_stop < min_stop_dist * 1.2:
                            mt5_stop = price - min_stop_dist * 1.2
                    else:
                        if mt5_stop - price < min_stop_dist * 1.2:
                            mt5_stop = price + min_stop_dist * 1.2

        request = {
            "action": int(mt5.TRADE_ACTION_DEAL),
            "symbol": str(broker_symbol),
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "sl": float(mt5_stop),
            "tp": 0.0,
            "deviation": int(config.execution.mt5.deviation_points),
            "magic": int(config.execution.mt5.magic),
            "comment": str(f"lzvis_{sig.id[:8]}"),
            "type_time": int(mt5.ORDER_TIME_GTC),
            "type_filling": filling,
        }

        log.info(
            f"[PIPELINE 4] order_send: {sig.action} {broker_symbol} "
            f"vol={request['volume']} ({vol_source} [{scenario}]) "
            f"price={request['price']} sl={request['sl']}"
        )

        result = await self._send_order(request)
        if result is None:
            err = await self._run_blocking(mt5.last_error)
            await self._log(f"order_send returned None: {err}", level="error")
            await self._publish_signal_failed(sig, f"order_send None: {err}")
            return

        done_codes = {mt5.TRADE_RETCODE_DONE}
        if hasattr(mt5, "TRADE_RETCODE_DONE_PARTIAL"):
            done_codes.add(mt5.TRADE_RETCODE_DONE_PARTIAL)
        if result.retcode not in done_codes:
            await self._log(
                f"Order rejected: retcode={result.retcode} comment={result.comment}",
                level="error",
            )
            await self._publish_signal_failed(
                sig, f"retcode={result.retcode} {result.comment}",
            )
            return

        # IMPORTANT: result.order is an ORDER ticket; the position has its
        # own ticket. Resolve the real one or every later close/poll fails.
        magic = int(config.execution.mt5.magic)
        position_ticket, fill_price = await self._resolve_position_ticket(
            result, broker_symbol, magic,
        )
        filled_volume = float(getattr(result, "volume", 0.0) or volume)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            await self._log(
                f"Partial fill on {broker_symbol}: "
                f"{filled_volume}/{volume} @ {fill_price}",
                level="warn",
            )
        if position_ticket is None:
            await self._log(
                f"Order filled but position ticket unresolved for "
                f"{broker_symbol} (order={getattr(result, 'order', None)}) — "
                f"leaving it to reconciliation; no local tracking.",
                level="error",
            )
            await self._publish_signal_failed(sig, "position ticket unresolved")
            return

        fill_price = float(fill_price or price)

        self._positions[sig.symbol] = {
            "ticket": int(position_ticket), "side": sig.action,
            "volume": filled_volume, "entry": fill_price,
            "opened_at": int(time.time() * 1000),
            "sl": float(mt5_stop), "tp": 0.0, "original_sl": float(mt5_stop),
            "pnl": 0.0, "swap": 0.0, "commission": 0.0,
            "current_price": fill_price,
            "scenario": scenario, "size_mult": size_mult,
            "vol_source": vol_source,
        }

        try:
            self._db.open_trade(
                signal_id=sig.id, entry_order_id=int(position_ticket),
                symbol=sig.symbol, broker_symbol=broker_symbol, side=sig.action,
                volume=filled_volume, entry_price=fill_price,
                entry_ts=int(time.time() * 1000), strategy=sig.strategy,
                status="open", entry_plan=entry_plan or None,
            )
        except Exception as exc:
            log.warning("DB open_trade failed: %s", exc)

        await self._log(
            f"Filled {sig.action} {broker_symbol} vol={filled_volume:.2f} "
            f"[{scenario} {vol_source}] @ {fill_price} "
            f"(position={position_ticket})"
        )
        await bus.publish(
            config.protocol.topics["position_update"],
            {
                "symbol": sig.symbol,
                "position": dict(self._positions[sig.symbol]),
                "ts": int(time.time() * 1000),
                "source": "fill",
            },
        )

    async def sync_stop(
        self, symbol: str, new_stop: float, reason: str = "trail",
    ) -> bool:
        """Push an updated stop loss to the broker for an open position.

        Strategies manage break-even and trailing stops in memory, but
        broker-side protection only changes if we send TRADE_ACTION_SLTP.
        Without this a crash leaves the original wide stop attached.
        """
        if not self._connected or new_stop <= 0:
            return False
        pos = self._positions.get(symbol)
        if pos is None:
            return False
        current = float(pos.get("sl") or 0.0)
        side = pos.get("side")
        # Stops are monotonic: only ever tighten.
        if side == "BUY" and new_stop <= current:
            return False
        if side == "SELL" and current > 0 and new_stop >= current:
            return False

        sym = registry.get(symbol)
        if sym is None or not sym.mt5:
            return False

        request = {
            "action": int(mt5.TRADE_ACTION_SLTP),
            "symbol": str(sym.mt5),
            "position": int(pos["ticket"]),
            "sl": float(new_stop),
            "tp": float(pos.get("tp") or 0.0),
        }
        result = await self._send_order(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = getattr(result, "retcode", "None")
            await self._log(
                f"Stop sync rejected for {symbol} ({reason}): "
                f"sl={new_stop:.4f} retcode={code}",
                level="warn",
            )
            return False

        pos["sl"] = float(new_stop)
        await self._log(
            f"Stop synced {symbol} ({reason}) → {new_stop:.4f}"
        )
        await bus.publish(
            config.protocol.topics["position_update"],
            {
                "symbol": symbol, "position": dict(pos),
                "ts": int(time.time() * 1000), "source": "sl_sync",
            },
        )
        return True

    async def _handle_close(self, sig: Signal, broker_symbol: str) -> None:
        pos = self._positions.get(sig.symbol)
        if pos is None:
            # Only ever consider OUR positions: positions_get(symbol=...)
            # returns manual and other-EA trades on the same symbol too.
            magic = int(config.execution.mt5.magic)
            positions = await self._run_blocking(
                mt5.positions_get, symbol=broker_symbol,
            ) or []
            ours = [
                p for p in positions
                if int(getattr(p, "magic", 0) or 0) == magic
            ]
            if not ours:
                await self._log(
                    f"CLOSE for {sig.symbol} but no open position with "
                    f"magic={magic}",
                    level="warn",
                )
                await self._publish_signal_failed(
                    sig, "close requested but no position found",
                )
                return
            p = max(ours, key=lambda p: int(getattr(p, "time", 0)))
            pos = {
                "ticket": int(p.ticket),
                "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": float(p.volume), "entry": float(p.price_open),
                "opened_at": int(p.time * 1000),
                "sl": float(getattr(p, "sl", 0.0) or 0.0),
                "tp": float(getattr(p, "tp", 0.0) or 0.0),
                "pnl": float(getattr(p, "profit", 0.0) or 0.0),
                "swap": float(getattr(p, "swap", 0.0) or 0.0),
                "commission": float(getattr(p, "commission", 0.0) or 0.0),
                "current_price": float(getattr(p, "price_current", 0.0) or 0.0),
            }
            self._positions[sig.symbol] = pos

        ticket = int(pos["ticket"])
        self._closing_tickets.add(ticket)

        try:
            tick = await self._run_blocking(mt5.symbol_info_tick, broker_symbol)
            if tick is None:
                await self._log(f"No tick for {broker_symbol}", level="error")
                return
            if pos["side"] == "BUY":
                close_type = int(mt5.ORDER_TYPE_SELL)
                close_price = float(tick.bid)
            else:
                close_type = int(mt5.ORDER_TYPE_BUY)
                close_price = float(tick.ask)
            filling = int(await self._detect_filling(broker_symbol))
            close_deviation = int(config.execution.mt5.deviation_points) * 3

            request = {
                "action": int(mt5.TRADE_ACTION_DEAL),
                "symbol": str(broker_symbol),
                "volume": float(pos["volume"]),
                "type": close_type,
                "position": ticket,
                "price": close_price,
                "sl": 0.0, "tp": 0.0,
                "deviation": close_deviation,
                "magic": int(config.execution.mt5.magic),
                "comment": str(f"close_{sig.id[:8]}"),
                "type_time": int(mt5.ORDER_TIME_GTC),
                "type_filling": filling,
            }

            result = await self._send_order(request)
            if result is None:
                err = await self._run_blocking(mt5.last_error)
                await self._log(f"close order_send None: {err}", level="error")
                return
            done_codes = {mt5.TRADE_RETCODE_DONE}
            if hasattr(mt5, "TRADE_RETCODE_DONE_PARTIAL"):
                done_codes.add(mt5.TRADE_RETCODE_DONE_PARTIAL)
            if result.retcode not in done_codes:
                await self._log(
                    f"Close rejected: retcode={result.retcode} comment={result.comment}",
                    level="error",
                )
                return

            pnl = 0.0
            try:
                deals = await self._run_blocking(mt5.history_deals_get, position=ticket)
                if deals:
                    for d in deals:
                        pnl += float(getattr(d, "profit", 0.0) or 0.0)
                        pnl += float(getattr(d, "swap", 0.0) or 0.0)
                        pnl += float(getattr(d, "commission", 0.0) or 0.0)
            except Exception:
                pass

            exit_reason = str((sig.metadata or {}).get("exit", "unknown"))
            exit_ts = int(time.time() * 1000)
            self._positions.pop(sig.symbol, None)
            entry_signal_id = self._lookup_entry_signal_id(ticket)

            await self._log(
                f"Closed {broker_symbol} ticket={pos['ticket']} "
                f"pnl={pnl:+.2f} reason={exit_reason}"
            )
            await bus.publish(
                config.protocol.topics["position_update"],
                {"symbol": sig.symbol, "position": None, "ts": exit_ts, "source": "close"},
            )
            await bus.publish(
                config.protocol.topics["trade_closed"],
                {
                    "symbol": sig.symbol, "ticket": int(pos["ticket"]),
                    "side": pos["side"], "volume": float(pos["volume"]),
                    "entry_price": float(pos["entry"]),
                    "exit_price": float(result.price or close_price),
                    "pnl": pnl, "pnl_pct": 0.0,
                    "exit_reason": exit_reason,
                    "signal_id": entry_signal_id,
                    "exit_order_id": int(result.order),
                    "ts": exit_ts,
                    "signal_metadata": dict(sig.metadata or {}),
                },
            )
        finally:
            self._closing_tickets.discard(ticket)

    async def _log(self, message: str, level: str = "info") -> None:
        await bus.publish(
            config.protocol.topics["log_emit"],
            {"level": level, "module": self.name, "message": message},
        )