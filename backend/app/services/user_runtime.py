"""Per-user strategy and monitor runtime registry."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from app.services.user_context import UserIdentity


@dataclass
class UserRuntime:
    user: UserIdentity
    strategy_engine: object
    monitor_engine: object


class UserRuntimeRegistry:
    def __init__(self, factory: Callable[[UserIdentity], UserRuntime]) -> None:
        self._factory = factory
        self._items: dict[str, UserRuntime] = {}
        self._lock = threading.RLock()

    def register(self, runtime: UserRuntime) -> None:
        with self._lock:
            self._items[runtime.user.id] = runtime

    def get(self, user: UserIdentity) -> UserRuntime:
        with self._lock:
            runtime = self._items.get(user.id)
            if runtime is None:
                runtime = self._factory(user)
                self._items[user.id] = runtime
            return runtime

    def remove(self, user_id: str) -> None:
        with self._lock:
            self._items.pop(user_id, None)

    def all(self) -> list[UserRuntime]:
        with self._lock:
            return list(self._items.values())


def runtime_for_request(request) -> UserRuntime | None:
    registry = getattr(request.app.state, "user_runtime_registry", None)
    request_state = getattr(request, "state", None)
    user = getattr(request_state, "user", None)
    if registry is None or user is None:
        return None
    return registry.get(user)
