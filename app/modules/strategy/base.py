from __future__ import annotations
import time, uuid
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
            "id": self.id, "strategy": self.strategy, "symbol": self.symbol,
            "action": self.action, "confidence": self.confidence, "reason": self.reason,
            "price": self.price, "metadata": self.metadata, "ts": self.ts,
        }

class Strategy(ABC):
    name: str = "unnamed"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.min_confidence: float = float(cfg.get("min_confidence", 0.5))

    @abstractmethod
    async def on_book(self, symbol: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> Signal | None:
        """Called on every book update for the active symbol."""

    async def on_trade(self, symbol: str, trade: dict[str, Any]) -> Signal | None:
        return None
        
    async def on_kline(self, symbol: str, payload: dict[str, Any]) -> Signal | None:
        return None

    def reset(self) -> None:
        pass