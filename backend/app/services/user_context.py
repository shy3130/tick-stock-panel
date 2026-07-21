"""Request-scoped user identity.

The calculation/data repository remains shared.  Only storage boundaries consult
this context, which keeps the upstream strategy and market-data code unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class UserIdentity:
    id: str
    username: str
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


_current: ContextVar[UserIdentity | None] = ContextVar("sycee_current_user", default=None)


def get_current_user() -> UserIdentity | None:
    return _current.get()


def require_current_user() -> UserIdentity:
    user = get_current_user()
    if user is None:
        raise RuntimeError("user context is not set")
    return user


def set_current_user(user: UserIdentity | None):
    return _current.set(user)


def reset(token) -> None:
    _current.reset(token)


@contextmanager
def as_user(user: UserIdentity | None) -> Iterator[None]:
    token = set_current_user(user)
    try:
        yield
    finally:
        reset(token)
