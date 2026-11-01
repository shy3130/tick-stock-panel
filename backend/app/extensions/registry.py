"""Backend extension registry with version checks and deterministic freezing."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Generic, TypeVar

from fastapi import APIRouter

from app.extensions.contracts import (
    BACKEND_EXTENSION_API_VERSION,
    DefaultNotificationFormatter,
    NotificationFormatter,
)

_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
T = TypeVar("T")


@dataclass(frozen=True)
class RegisteredImplementation(Generic[T]):
    extension_id: str
    implementation_id: str
    implementation: T
    order: int


class BackendExtensionRegistrar:
    """Staging area: a failed setup is discarded without partial registration."""

    def __init__(self, extension_id: str, *, api_version: int) -> None:
        self.extension_id = extension_id
        self.api_version = api_version
        self.routers: list[APIRouter] = []
        self.notification_formatters: list[tuple[str, NotificationFormatter, int]] = []

    def include_router(self, router: APIRouter) -> None:
        if not isinstance(router, APIRouter):
            raise TypeError("router must be fastapi.APIRouter")
        self.routers.append(router)

    def register_notification_formatter(
        self,
        implementation_id: str,
        formatter: NotificationFormatter,
        *,
        order: int = 100,
    ) -> None:
        self.notification_formatters.append((implementation_id, formatter, order))


class BackendExtensionRegistry:
    def __init__(self) -> None:
        self._extension_ids: set[str] = set()
        self._notification_formatters: list[RegisteredImplementation[NotificationFormatter]] = []
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def has_customizations(self) -> bool:
        return bool(self._extension_ids)

    @property
    def has_notification_formatters(self) -> bool:
        return bool(self._notification_formatters)

    def extension_ids(self) -> frozenset[str]:
        return frozenset(self._extension_ids)

    def register(self, registrar: BackendExtensionRegistrar) -> None:
        """Validate a staged extension fully before mutating the registry."""
        self._ensure_mutable()
        extension_id = registrar.extension_id
        self._validate_id(extension_id, "extension_id")
        if registrar.api_version != BACKEND_EXTENSION_API_VERSION:
            raise ValueError(
                f"extension {extension_id!r} requires backend API v{registrar.api_version}; "
                f"current is v{BACKEND_EXTENSION_API_VERSION}"
            )
        if extension_id in self._extension_ids:
            raise ValueError(f"duplicate extension id: {extension_id}")

        known_ids = {item.implementation_id for item in self._notification_formatters}
        staged_ids: set[str] = set()
        staged: list[RegisteredImplementation[NotificationFormatter]] = []
        for implementation_id, formatter, order in registrar.notification_formatters:
            self._validate_id(implementation_id, "implementation_id")
            if not isinstance(formatter, NotificationFormatter):
                raise TypeError("formatter must inherit NotificationFormatter")
            if formatter.api_version != BACKEND_EXTENSION_API_VERSION:
                raise ValueError(
                    f"formatter {implementation_id!r} requires API v{formatter.api_version}; "
                    f"current is v{BACKEND_EXTENSION_API_VERSION}"
                )
            if implementation_id in known_ids or implementation_id in staged_ids:
                raise ValueError(f"duplicate notification formatter id: {implementation_id}")
            staged_ids.add(implementation_id)
            staged.append(
                RegisteredImplementation(extension_id, implementation_id, formatter, order)
            )

        self._extension_ids.add(extension_id)
        self._notification_formatters.extend(staged)

    def freeze(self) -> None:
        self._notification_formatters.sort(
            key=lambda item: (item.order, item.implementation_id)
        )
        self._frozen = True

    def mutable_copy(self) -> BackendExtensionRegistry:
        """Stage an optional package without mutating the published snapshot."""
        registry = BackendExtensionRegistry()
        registry._extension_ids = self._extension_ids.copy()
        registry._notification_formatters = self._notification_formatters.copy()
        return registry

    def notification_formatters(
        self,
    ) -> tuple[RegisteredImplementation[NotificationFormatter], ...]:
        if not self._frozen:
            raise RuntimeError("backend extension registry must be frozen before use")
        if not self._notification_formatters:
            return (
                RegisteredImplementation(
                    "core", "core.notification", DefaultNotificationFormatter(), 0,
                ),
            )
        return tuple(self._notification_formatters)

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("backend extension registry is frozen")

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise ValueError(f"invalid {label}: {value!r}")
