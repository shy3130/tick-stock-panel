"""Wire declarations shared by signed data sources and the data engine."""

from pydantic import Field, model_validator

from .base import Contract, Key, UTCDateTime
from .data import AssetClass, KnowledgeBasis, PriceBasis
from .extension_fields import ExtensionField, validate_field_universe
from .facts import RecordType


class SyncRequest(Contract):
    dataset_id: Key
    record_type: RecordType
    market_id: Key
    asset_class: AssetClass
    instrument_ids: tuple[Key, ...]
    extension_field: ExtensionField | None = Field(default=None, exclude_if=lambda v: v is None)
    start: UTCDateTime
    end: UTCDateTime
    timeframe: Key | None = None
    price_basis: PriceBasis | None = None

    @model_validator(mode="after")
    def coherent(self):
        if (self.record_type == "extension_value") != (self.extension_field is not None):
            raise ValueError("extension requests must identify one versioned field")
        validate_field_universe(self.extension_field, self.instrument_ids)
        if self.end <= self.start or len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise ValueError("nonempty range and unique instruments required")
        if self.record_type == "bar" and (self.timeframe is None or self.price_basis is None):
            raise ValueError("bar requests must specify timeframe and price basis")
        if self.record_type != "bar" and (
            self.timeframe is not None or self.price_basis is not None
        ):
            raise ValueError("timeframe and price basis apply only to bar requests")
        return self


class ProviderCapability(Contract):
    record_type: RecordType
    market_id: Key
    asset_class: AssetClass
    timeframes: tuple[Key, ...] = ()
    price_bases: tuple[PriceBasis, ...] = ()
    knowledge_basis: KnowledgeBasis
    normalization_version: Key
    bar_convention_id: Key | None = None

    def supports(self, request: SyncRequest) -> bool:
        return (
            self.record_type == request.record_type
            and self.market_id == request.market_id
            and self.asset_class == request.asset_class
            and (
                request.record_type != "bar"
                or (
                    request.timeframe in self.timeframes
                    and request.price_basis in self.price_bases
                    and self.bar_convention_id is not None
                )
            )
        )


class ProviderDescriptor(Contract):
    provider_id: Key
    version: Key
    source_ids: tuple[Key, ...] = Field(min_length=1)
    capabilities: tuple[ProviderCapability, ...] = Field(min_length=1)
