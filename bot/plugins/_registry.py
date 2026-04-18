from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

Handler = Callable[..., Coroutine[Any, Any, None]]


class ActionRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, action: str, handler: Handler) -> None:
        if action in self._handlers:
            raise ValueError(f"Action collision: '{action}' already registered")
        self._handlers[action] = handler

    def get(self, action: str) -> Handler | None:
        return self._handlers.get(action)

    def all_actions(self) -> list[str]:
        return list(self._handlers.keys())


class ScopedActionRegistry:
    """Action registry facade that binds handlers to one plugin context.

    Plugin handlers are registered against the shared base action registry, but
    when invoked they receive this plugin's context instead of the callback-time
    base context.
    """

    def __init__(self, base_registry: ActionRegistry, ctx_provider: Callable[[], Any]) -> None:
        self._base = base_registry
        self._ctx_provider = ctx_provider

    def register(self, action: str, handler: Handler) -> None:
        async def _bound_handler(query, parts, _base_ctx) -> None:
            await handler(query, parts, self._ctx_provider())

        self._base.register(action, _bound_handler)

    def get(self, action: str) -> Handler | None:
        return self._base.get(action)

    def all_actions(self) -> list[str]:
        return self._base.all_actions()


class ButtonRegistry:
    def __init__(self) -> None:
        self._buttons: list[tuple[str, str, int]] = []  # (label, callback_data, sort_key)

    def add(self, label: str, callback_data: str, sort_key: int = 100) -> None:
        self._buttons.append((label, callback_data, sort_key))

    def sorted_buttons(self) -> list[tuple[str, str]]:
        return [(label, cb) for label, cb, _ in sorted(self._buttons, key=lambda x: x[2])]
