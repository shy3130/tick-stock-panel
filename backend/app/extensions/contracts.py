"""Stable, small-grained contracts for in-repository secondary development."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

BACKEND_EXTENSION_API_VERSION = 1


class RepositoryAccess(Protocol):
    """Read-oriented repository surface exposed to backend extensions."""

    def get_name_map(self, symbols: list[str] | None = None) -> dict[str, str]: ...


@dataclass(frozen=True)
class ExtensionContext:
    api_version: int
    data_dir: Path
    repository: RepositoryAccess


@dataclass(frozen=True)
class NotificationFormatContext:
    api_version: int


class NotificationFormatter(ABC):
    """Customize notification copy without changing the event schema or semantics."""

    api_version = BACKEND_EXTENSION_API_VERSION

    @abstractmethod
    def format_message(
        self,
        event: dict[str, Any],
        context: NotificationFormatContext,
    ) -> str:
        """Return notification copy. The input event must not be mutated."""
        raise NotImplementedError


class DefaultNotificationFormatter(NotificationFormatter):
    def format_message(
        self,
        event: dict[str, Any],
        context: NotificationFormatContext,
    ) -> str:
        del context
        return str(event.get("message") or "")


@dataclass(frozen=True)
class ExtCollectorContext:
    """Read-oriented execution context passed to an ExtCollector."""

    api_version: int
    data_dir: Path
    target_date: date
    config_id: str
    repository: RepositoryAccess | None = None
    is_test: bool = False


@dataclass(frozen=True)
class ExtCollectorResult:
    """Structured output of an ExtCollector run.

    - ``allow_empty=True`` allows writing an empty typed partition so daily
      timeseries tables distinguish "scanned with 0 hits" from "not scanned".
    - ``partial_errors`` preserves succeeded rows while recording failure
      details (status ``"partial"``) rather than reporting a clean empty run.
    - ``skipped_reason`` skips writing any partition and records ``"skipped"``.
    """

    rows: tuple[dict[str, Any], ...] = ()
    message: str = ""
    allow_empty: bool = True
    partial_errors: tuple[str, ...] = ()
    skipped_reason: str | None = None
    replace_partition: bool = True


class ExtCollector(ABC):
    """Scheduled or on-demand data collector plugged into ext_data PullScheduler."""

    api_version = BACKEND_EXTENSION_API_VERSION
    label: str = ""
    description: str = ""

    @abstractmethod
    def collect(self, context: ExtCollectorContext) -> ExtCollectorResult:
        """Collect rows for ``context.target_date`` (executed in a worker thread)."""
        raise NotImplementedError
