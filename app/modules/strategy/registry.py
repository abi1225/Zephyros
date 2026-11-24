from __future__ import annotations

import importlib
import logging

from app.modules.strategy.base import Strategy

log = logging.getLogger(__name__)

_STRATEGIES: dict[str, str] = {
    "tape_reader": "app.modules.strategy.tape_reader.TapeReader",
    "four_pattern": "app.modules.strategy.four_pattern.FourPattern",
}

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {}

for name, dotted in _STRATEGIES.items():
    try:
        module_path, class_name = dotted.rsplit(".", 1)
        module = importlib.import_module(module_path)
        STRATEGY_REGISTRY[name] = getattr(module, class_name)
    except Exception as exc:
        log.warning("Strategy %r could not be loaded: %s", name, exc)