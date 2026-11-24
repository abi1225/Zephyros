from __future__ import annotations

from abc import ABC, abstractmethod


class BaseModule(ABC):
    """Every backend module implements this. Modules are started in
    order during app boot and stopped in reverse on shutdown."""

    name: str = "unnamed"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...