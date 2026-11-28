"""Explicit upstream inputs, captured before the research task is queued."""

from typing import Literal, Self

from pydantic import Field, model_validator
from tivelo_backtest.runner import RunRequest
from tivelo_contracts.base import CalendarDate, Contract, Digest, Key, RelativePath
from tivelo_contracts.data import Coverage, DatasetReference, KnowledgeBasis
from tivelo_contracts.execution import (
    DefinitionReference,
    ExtensionPin,
    PackageReference,
    RuntimeReference,
)
from tivelo_data.adapters.cn_daily import (
    CNRawDailyRequest,
    CNSymbolRequest,
    RepositoryDailyRequest,
    RepositoryFactorRequest,
    RepositoryMinuteRequest,
)
from tivelo_data.adapters.cn_daily import DailySymbol as DailySymbol
from tivelo_data.native_source import NativeDataInput
from tivelo_data.providers import SyncPlan, SyncRequest
from tivelo_data.storage import Candidate

try:
    from tivelo_data.adapters.cn_daily import CNFactorRequest
except ImportError:
    # Earlier Data wheels can still read a stored workbench request after rollback.
    # Their archive adapter supplies the same scope validation.
    class CNFactorRequest(CNSymbolRequest):
        representation: Literal["event_ratio", "cumulative_forward"]
        baseline_date: CalendarDate | None = None

        @model_validator(mode="after")
        def factor_scope(self) -> Self:
            values = self.model_dump(exclude={"kind"})
            RepositoryFactorRequest.model_validate({**values, "layout": "event_factors_v1"})
            return self


class LocalDataInput(Contract):
    kind: Literal["local_jsonl"] = "local_jsonl"
    path: RelativePath
    request: SyncRequest
    source_id: Key
    knowledge_basis: KnowledgeBasis
    bar_convention_id: Key | None = None
    coverage: Coverage | None = None


class RoutedDailyInput(CNRawDailyRequest):
    kind: Literal["tsp_daily"]


try:
    from tivelo_data.adapters.cn_daily import CNRawMinuteRequest
except ImportError:
    class CNRawMinuteRequest(CNSymbolRequest):
        calendar_version: Key

        @model_validator(mode="after")
        def minute_scope(self) -> Self:
            values = self.model_dump(exclude={"kind"})
            RepositoryMinuteRequest.model_validate({**values, "layout": "minute_v1", "minute_label": "start"})
            return self


class RoutedMinuteInput(CNRawMinuteRequest):
    kind: Literal["tsp_minute"]


class RoutedFactorInput(CNFactorRequest):
    kind: Literal["tsp_factors"]


class RepositoryDailyInput(RepositoryDailyRequest):
    kind: Literal["tsp_repository_daily"]


class RepositoryFactorInput(RepositoryFactorRequest):
    kind: Literal["tsp_repository_factors"]


class RepositoryMinuteInput(RepositoryMinuteRequest):
    kind: Literal["tsp_repository_minute"]


# Use ordinary union validation so stored inputs without a kind still select local_jsonl.
DataInput = (
    LocalDataInput
    | NativeDataInput
    | RoutedDailyInput
    | RoutedMinuteInput
    | RoutedFactorInput
    | RepositoryDailyInput
    | RepositoryFactorInput
    | RepositoryMinuteInput
)


class CaptureRequest(Contract):
    datasets: tuple[DataInput, ...] = Field(default=(), max_length=16)
    versions: tuple[DatasetReference, ...] = Field(
        default=(), max_length=16, exclude_if=lambda v: not v
    )

    @model_validator(mode="after")
    def unique_datasets(self) -> Self:
        identities = [source.request.dataset_id for source in self.datasets]
        pinned = [version.dataset_id for version in self.versions]
        if not 1 <= len(identities) + len(pinned) <= 16:
            raise ValueError("a snapshot requires between one and sixteen datasets")
        if (
            identities != sorted(set(identities))
            or pinned != sorted(set(pinned))
            or set(identities) & set(pinned)
        ):
            raise ValueError("prepared dataset identities must be sorted and unique")
        return self


class PreparationRequest(CaptureRequest):
    definition: RelativePath
    dependency_root: RelativePath | None = None
    run: RunRequest


class LockedSource(Contract):
    source: DataInput
    plan: SyncPlan

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.source.request != self.plan.request:
            raise ValueError("locked source selection differs from its requested scope")
        if isinstance(self.source, NativeDataInput):
            if (
                self.plan.extension is None
                or self.source.provider_id != self.plan.extension.capability_id
            ):
                raise ValueError("locked native source differs from its signed capability")
        elif self.plan.extension is not None:
            raise ValueError("unexpected data extension on a non-native source")
        return self


class LockedCapture(Contract):
    sources: tuple[LockedSource, ...]
    requirements: tuple[str, ...]
    packages: tuple[PackageReference, ...]
    runtime: RuntimeReference


class LockedPreparation(LockedCapture):
    definition: DefinitionReference
    dependencies: tuple[DefinitionReference, ...]
    price_extension: ExtensionPin | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    extension_requirements: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class PreparationReceipt(Contract):
    format: Literal["tsp-engine-preparation"] = "tsp-engine-preparation"
    version: Literal["1.0"] = "1.0"
    plan_id: Digest
    node_id: str
    payload: Candidate
