from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.core.config import config


def override_path() -> Path:
    return Path(config.app.symbols_override_path)


def save_symbols(symbols: list[dict[str, Any]]) -> None:
    """Write the full symbol list to the override file.

    The file is gitignored. Deleting it reverts to whatever
    default.yaml (and any env/local overrides) specify.
    """
    path = override_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Strip None values so the YAML stays readable.
    cleaned: list[dict[str, Any]] = []
    for s in symbols:
        cleaned.append({k: v for k, v in s.items() if v is not None})

    payload = {"symbols": cleaned}
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            payload,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


def load_symbols() -> list[dict[str, Any]]:
    """Read the override file if it exists, else empty list."""
    path = override_path()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("symbols", []) or []


def clear_overrides() -> None:
    path = override_path()
    if path.exists():
        path.unlink()