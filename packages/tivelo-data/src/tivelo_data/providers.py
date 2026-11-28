"""Provider injection, bounded pages and locked source selection.

The workbench supplies its existing capability router through SourceResolver.
There is intentionally no second global vendor routing registry here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import Field, model_validator
from tivelo_contracts.base import Contract, Digest, fingerprint
from tivelo_contracts.data import Coverage
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import ExtensionPin
from tivelo_contracts.providers import ProviderCapability, ProviderDescriptor, SyncRequest


@dataclass(frozen=True)
class FetchPage:
    records: tuple[Contract, ...]
    next_cursor: str | None
    # Final page supplies coverage for the entire request, not just its own rows.
    coverage: Coverage | None = None


@dataclass(frozen=True)
class SyncContext:
    resolve_credentials: Callable[[str], Mapping[str, str]]
    cancelled: Callable[[], bool]
    progress: Callable[[int, str], None]

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise TiveloError(ErrorCode.CANCELLED, "Data synchronization cancelled")


class Provider(Protocol):
    def describe(self) -> ProviderDescriptor: ...

    def check_ready(self, context: SyncContext) -> None: ...

    def fetch(
        self, request: SyncRequest, cursor: str | None, context: SyncContext
    ) -> FetchPage: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ResolvedSource:
    provider: Provider
    configuration_digest: str
    extension: ExtensionPin | None = None


class SourceResolver(Protocol):
    def resolve(self, request: SyncRequest) -> ResolvedSource: ...


def close_source(provider: Provider, *, primary: BaseException | None = None) -> None:
    """Keep a fetch or selection failure authoritative when cleanup also fails."""
    try:
        provider.close()
    except Exception as exc:
        if primary is not None:
            primary.add_note("The source also failed to close")
        elif isinstance(exc, TiveloError):
            raise
        else:
            raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "Data source failed to close") from None


class SyncPlan(Contract):
    request: SyncRequest
    provider: ProviderDescriptor
    capability: ProviderCapability
    source_configuration_digest: Digest
    extension: ExtensionPin | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def selected_capability(self):
        if self.capability not in self.provider.capabilities or not self.capability.supports(
            self.request
        ):
            raise ValueError("plan capability is not declared by its selected source")
        if self.extension and (
            self.extension.kind != "dataset_provider"
            or self.extension.capability_id != self.provider.provider_id
            or self.extension.version != self.provider.version
        ):
            raise ValueError("data extension differs from the selected provider")
        return self

    @property
    def plan_id(self) -> str:
        return fingerprint(self)


def plan_source(request: SyncRequest, source: ResolvedSource) -> SyncPlan:
    descriptor = source.provider.describe()
    capabilities = [c for c in descriptor.capabilities if c.supports(request)]
    if len(capabilities) != 1:
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Source has no unambiguous capability for this request",
        )
    return SyncPlan(
        request=request,
        provider=descriptor,
        capability=capabilities[0],
        source_configuration_digest=source.configuration_digest,
        extension=getattr(source, "extension", None),
    )


class FixedSourceResolver:
    """Explicit single source for a standalone consumer; no implicit fallback."""

    def __init__(self, provider: Provider, configuration_digest: str):
        self._source = ResolvedSource(
            provider, configuration_digest, getattr(provider, "extension_pin", None)
        )

    def resolve(self, request: SyncRequest) -> ResolvedSource:
        return self._source


def local_context() -> SyncContext:
    def no_credentials(reference: str) -> Mapping[str, str]:
        raise TiveloError(ErrorCode.AUTHENTICATION_FAILED, "No credential resolver configured")

    return SyncContext(no_credentials, lambda: False, lambda count, stage: None)
