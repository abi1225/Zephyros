from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Signal:
    strategy: str
    symbol: str
    action: str
    confidence: float
    reason: str
    price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "action": self.action,
            "confidence": self.confidence,
            "reason": self.reason,
            "price": self.price,
            "metadata": self.metadata,
            "ts": self.ts,
        }


class Strategy(ABC):
    """Symbol-agnostic strategy base.

    Strategies hold per-symbol state keyed by symbol ID and resolve
    tunables from `per_symbol.<symbol>.*` overrides, falling back to
    base config. On symbol switch the manager calls `set_symbol()`,
    which refreshes cached params via `_apply_params()` and resets
    transient state — the instance itself survives across symbols.
    """

    name: str = "unnamed"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        # Split base vs per-symbol so `_param()` can resolve properly.
        self._base_cfg: dict[str, Any] = {
            k: v for k, v in cfg.items() if k != "per_symbol"
        }
        self._per_symbol_cfg: dict[str, dict] = cfg.get("per_symbol", {}) or {}
        self.min_confidence: float = float(cfg.get("min_confidence", 0.5))

    # ─── Per-symbol param resolution ─────────────────────────

    def _param(self, name: str, symbol: str | None, default: Any = None) -> Any:
        """Resolve a tunable with per-symbol override.

        Lookup order:
          1. per_symbol.<symbol>.<name>      (most specific)
          2. base config <name>
          3. default
        """
        if symbol:
            overrides = self._per_symbol_cfg.get(symbol, {})
            if name in overrides:
                return overrides[name]
        return self._base_cfg.get(name, default)

    def _apply_params(self, symbol: str) -> None:
        """Hook: reload cached params for `symbol`. Override in subclasses."""
        return

    # ─── Abstract feed handlers ──────────────────────────────

    @abstractmethod
    async def on_book(
        self,
        symbol: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> Signal | None:
        """Called on every book update for the active symbol."""

    async def on_trade(
        self, symbol: str, trade: dict[str, Any]
    ) -> Signal | None:
        return None

    async def on_kline(
        self, symbol: str, payload: dict[str, Any]
    ) -> Signal | None:
        return None

    # ─── Lifecycle hooks (default no-ops) ────────────────────

    def set_symbol(self, symbol: str) -> None:
        self._active_symbol = symbol
        self._apply_params(symbol)

    def set_timeframe(self, timeframe: str) -> None:
        self._timeframe = timeframe

    def reset(self) -> None:
        pass

    def reset_symbol(self, symbol: str) -> None:
        pass

    def reset_position_state(self) -> None:
        pass

    def has_open_position(self) -> bool:
        return False

    def dispose(self) -> None:
        pass