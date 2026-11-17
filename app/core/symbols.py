from __future__ import annotations

from typing import Iterable

from app.core.config import SymbolCfg, config


class SymbolRegistry:
    """Single lookup for every symbol in the app.

    Indexes by canonical ID, Binance name, and MT5 name. Modules
    inside the app use canonical IDs exclusively; the feed and
    executor translate at the edges.
    
    Also tracks the currently-active symbol (driven by the UI
    dropdown). Feeds only publish for the active symbol.
    """

    def __init__(self, symbols: Iterable[SymbolCfg]) -> None:
        self._by_id: dict[str, SymbolCfg] = {}
        self._by_binance: dict[str, SymbolCfg] = {}
        self._by_mt5: dict[str, SymbolCfg] = {}
        self._active_id: str | None = None

        for s in symbols:
            self._register(s)

        # Default active symbol = first enabled
        for s in symbols:
            if s.enabled:
                self._active_id = s.id
                break

    # ─── Indexing ───────────────────────────────────────────

    def _register(self, s: SymbolCfg) -> None:
        self._by_id[s.id] = s
        if s.binance:
            self._by_binance[s.binance] = s
        if s.mt5:
            self._by_mt5[s.mt5] = s

    def _rebuild_reverse_indexes(self) -> None:
        self._by_binance = {
            s.binance: s for s in self._by_id.values() if s.binance
        }
        self._by_mt5 = {
            s.mt5: s for s in self._by_id.values() if s.mt5
        }

    # ─── Active Symbol Tracking ─────────────────────────────

    def set_active(self, symbol_id: str) -> None:
        """Set the currently-active symbol (called by the UI dropdown)."""
        if symbol_id in self._by_id:
            self._active_id = symbol_id

    def get_active(self) -> str | None:
        return self._active_id

    def get_active_cfg(self) -> SymbolCfg | None:
        if not self._active_id:
            return None
        return self._by_id.get(self._active_id)

    # ─── Canonical-ID access ────────────────────────────────

    def get(self, symbol_id: str) -> SymbolCfg | None:
        return self._by_id.get(symbol_id)

    def require(self, symbol_id: str) -> SymbolCfg:
        s = self._by_id.get(symbol_id)
        if s is None:
            raise KeyError(f"unknown symbol id: {symbol_id!r}")
        return s

    def all(self) -> list[SymbolCfg]:
        return list(self._by_id.values())

    def enabled(self) -> list[SymbolCfg]:
        return [s for s in self._by_id.values() if s.enabled]

    def ids(self) -> list[str]:
        return list(self._by_id.keys())

    def enabled_ids(self) -> list[str]:
        return [s.id for s in self._by_id.values() if s.enabled]

    # ─── Reverse lookup ─────────────────────────────────────

    def by_binance(self, binance_symbol: str) -> SymbolCfg | None:
        return self._by_binance.get(binance_symbol)

    def by_mt5(self, mt5_symbol: str) -> SymbolCfg | None:
        return self._by_mt5.get(mt5_symbol)

    def to_id(self, any_symbol: str) -> str | None:
        if any_symbol in self._by_id:
            return any_symbol
        s = self._by_binance.get(any_symbol) or self._by_mt5.get(any_symbol)
        return s.id if s else None

    # ─── Translate canonical → exchange name ────────────────

    def to_binance(self, symbol_id: str) -> str | None:
        s = self._by_id.get(symbol_id)
        return s.binance if s else None

    def to_ctrader(self, symbol_id: str) -> str | None:
        s = self._by_id.get(symbol_id)
        return s.ctrader if s else None

    def to_mt5(self, symbol_id: str) -> str | None:
        s = self._by_id.get(symbol_id)
        return s.mt5 if s else None

    def display(self, symbol_id: str) -> str:
        s = self._by_id.get(symbol_id)
        if s is None:
            return symbol_id
        return s.display or s.id

    # ─── Runtime mutation ───────────────────────────────────

    def reload(self, symbols: Iterable[SymbolCfg]) -> None:
        self._by_id.clear()
        self._by_binance.clear()
        self._by_mt5.clear()
        for s in symbols:
            self._register(s)

    def to_list(self) -> list[dict]:
        return [s.model_dump() for s in self._by_id.values()]

    # ─── Validation ─────────────────────────────────────────

    def validate(self) -> list[str]:
        errors: list[str] = []
        seen_ids: set[str] = set()
        seen_binance: dict[str, str] = {}
        seen_mt5: dict[str, str] = {}

        for s in self._by_id.values():
            if not s.id:
                errors.append("A symbol has an empty id.")
                continue
            if s.id in seen_ids:
                errors.append(f"Duplicate symbol id: {s.id!r}")
            seen_ids.add(s.id)

            if s.binance:
                if s.binance in seen_binance:
                    errors.append(
                        f"Binance name {s.binance!r} is used by both "
                        f"{seen_binance[s.binance]!r} and {s.id!r}"
                    )
                seen_binance[s.binance] = s.id

            if s.mt5:
                if s.mt5 in seen_mt5:
                    errors.append(
                        f"MT5 name {s.mt5!r} is used by both "
                        f"{seen_mt5[s.mt5]!r} and {s.id!r}"
                    )
                seen_mt5[s.mt5] = s.id

        return errors


# Module-level singleton
registry = SymbolRegistry(config.symbols)