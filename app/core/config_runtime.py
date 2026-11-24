from __future__ import annotations

from typing import Any

from app.core.config import BotConfig, config
from app.core.event_bus import bus


_current: BotConfig = config
_overrides: dict[str, Any] = {}


def _set_path(d: dict, path: list[str], value: Any) -> None:
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def get_current() -> BotConfig:
    return _current


def to_dict() -> dict:
    return _current.model_dump()


async def apply_updates(updates: dict[str, Any]) -> BotConfig:
    global _current, _overrides
    base = _current.model_dump()
    for dotted, value in updates.items():
        _set_path(base, dotted.split("."), value)
        _set_path(_overrides, dotted.split("."), value)

    _current = BotConfig.model_validate(base)
    topic = _current.protocol.topics["config_updated"]
    await bus.publish(topic, _current.model_dump())
    return _current