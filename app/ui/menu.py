from __future__ import annotations

from typing import Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMessageBox

from app.core.config import config

Handler = Callable[[], None]


class MenuBarBuilder:
    """Builds a QMenuBar from `ui.menu` in the config.

    Actions are looked up by their `action` string. Register new
    handlers with `register(action_id, fn)` before calling `build()`.
    Unknown actions show a "not wired" message instead of crashing.
    """

    def __init__(self, window: QMainWindow) -> None:
        self.window = window
        self.menu_bar = window.menuBar()
        self._handlers: dict[str, Handler] = {}

    def register(self, action_id: str, handler: Handler) -> None:
        self._handlers[action_id] = handler

    def build(self) -> None:
        for menu_cfg in config.ui.menu:
            menu = self.menu_bar.addMenu(menu_cfg.label)
            for item in menu_cfg.items:
                action = QAction(item.label, self.window)
                action.triggered.connect(
                    lambda _=False, a=item.action: self._dispatch(a)
                )
                menu.addAction(action)

    def _dispatch(self, action_id: str) -> None:
        handler = self._handlers.get(action_id)
        if handler is None:
            QMessageBox.information(
                self.window,
                "Not wired",
                f"Action {action_id!r} has no handler yet.",
            )
            return
        handler()