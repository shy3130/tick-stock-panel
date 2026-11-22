"""Offline CN auction quantity rules, separate from complete order admission."""

from __future__ import annotations

from datetime import UTC, datetime, time
from importlib import resources
from typing import Literal, Self

from pydantic import Field, model_validator
from tivelo_contracts.base import (
    CalendarDate,
    Contract,
    Digest,
    Key,
    UTCDateTime,
    fingerprint,
    utc_datetime,
)
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import OrderQuantityTerms

from .calendar import CN_TIMEZONE
from .classification import Board, CNEquityClass
from .presets import CalendarSource, Venue

_BUILTIN_DIGESTS = {
    "2026.1.0": "15c44873efb8c73704b003b3380c43f945d9ba21dade659e2a4a519c84b54ce2",
    "2026.2.0": "185b0983798ff071f0bb24288f764b60e515c1b4a5c8acfc8d8f404f8e3fec00",
}


class QuantitySchedule(Contract):
    rule_id: Key
    venue_id: Venue
    board: Board
    effective_from: CalendarDate
    source_ids: tuple[Key, ...] = Field(min_length=1)
    clauses: tuple[Key, ...] = Field(min_length=1)
    order_quantities: tuple[OrderQuantityTerms, ...] = Field(min_length=1)
    market_requires_price_limits: bool = Field(strict=True)
    risk_warning_limit_only: bool = Field(strict=True)
    delisting_limit_only: bool = Field(strict=True)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        CNEquityClass(
            venue_id=self.venue_id, board=self.board, risk_status="normal", listing_phase="regular"
        )
        if tuple(q.order_type for q in self.order_quantities) != ("limit", "market"):
            raise ValueError("quantity schedules explicitly define limit and market quantities")
        if len(set(self.source_ids)) != len(self.source_ids) or len(set(self.clauses)) != len(
            self.clauses
        ):
            raise ValueError("source and clause references must be unique")
        return self


class QuantityPresetContents(Contract):
    schema_version: Literal["1.0"] = "1.0"
    preset_id: Literal["cn.equity.auction-quantities"] = "cn.equity.auction-quantities"
    preset_version: Key
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    scope: Literal["continuous_auction_quantity_and_type"] = "continuous_auction_quantity_and_type"
    coverage_start: CalendarDate
    coverage_end: CalendarDate
    verified_on: CalendarDate
    sources: tuple[CalendarSource, ...] = Field(min_length=1)
    schedules: tuple[QuantitySchedule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.coverage_end < self.coverage_start:
            raise ValueError("quantity coverage is inclusive and ordered")
        sources = {source.source_id for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("source identifiers must be unique")
        if len({rule.rule_id for rule in self.schedules}) != len(self.schedules):
            raise ValueError("quantity rule identifiers must be unique")
        if len({(r.venue_id, r.board, r.effective_from) for r in self.schedules}) != len(
            self.schedules
        ):
            raise ValueError("quantity rule epochs must be unique within each board")
        if any(set(rule.source_ids) - sources for rule in self.schedules):
            raise ValueError("quantity rule source is missing")
        if any(rule.effective_from > self.coverage_end for rule in self.schedules):
            raise ValueError("quantity rule starts outside coverage")
        return self


class QuantityPreset(Contract):
    preset_format: Literal["tivelo-cn-quantity-preset-v1"] = "tivelo-cn-quantity-preset-v1"
    version_id: Digest
    contents: QuantityPresetContents

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.version_id != fingerprint(self.contents):
            raise ValueError("quantity preset content digest does not match")
        return self

    @classmethod
    def create(cls, contents: QuantityPresetContents) -> Self:
        contents = QuantityPresetContents.model_validate_json(contents.model_dump_json())
        return cls(version_id=fingerprint(contents), contents=contents)


class QuantitySelection(Contract):
    scope: Literal["continuous_auction_quantity_and_type"] = "continuous_auction_quantity_and_type"
    preset_version_id: Digest
    rule_id: Key
    spec_revision: Key
    effective_from: CalendarDate
    available_at: UTCDateTime
    source_ids: tuple[Key, ...]
    clauses: tuple[Key, ...]
    order_quantities: tuple[OrderQuantityTerms, ...]


def load_quantity_preset(*, version: str, expected_sha256: str | None = None) -> QuantityPreset:
    """Load a pinned built-in release without network access or latest-version resolution."""
    expected = _BUILTIN_DIGESTS.get(version)
    if expected is None:
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Unknown CN quantity preset version")
    if expected_sha256 is not None and expected_sha256 != expected:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Quantity preset differs from the pinned digest"
        )
    try:
        payload = (
            resources.files("tivelo_market_cn")
            .joinpath("quantity_presets", f"{version}.json")
            .read_bytes()
        )
        preset = QuantityPreset.model_validate_json(payload)
    except (OSError, ValueError) as exc:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Installed quantity preset is missing or invalid"
        ) from exc
    if preset.version_id != expected or preset.contents.preset_version != version:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Installed quantity preset was replaced")
    return preset


def resolve_quantity_rules(
    preset: QuantityPreset, classification: CNEquityClass, *, at: datetime
) -> QuantitySelection:
    """Resolve numeric/type constraints, not sessions, price protection or account permission."""
    at = utc_datetime(at)
    preset = QuantityPreset.model_validate_json(preset.model_dump_json())
    classification = CNEquityClass.model_validate_json(classification.model_dump_json())
    contents = preset.contents
    day = at.astimezone(CN_TIMEZONE).date()
    if not contents.coverage_start <= day <= contents.coverage_end:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Date is outside quantity preset coverage"
        )
    if classification.board == "star" and classification.listing_phase == "relisting_first":
        raise TiveloError(
            ErrorCode.UNSUPPORTED_ASSET, "STAR relisting quantity policy is not covered"
        )
    sources = {source.source_id: source for source in contents.sources}
    candidates = []
    for rule in contents.schedules:
        if (rule.venue_id, rule.board) != (classification.venue_id, classification.board):
            continue
        effective = datetime.combine(rule.effective_from, time(), CN_TIMEZONE).astimezone(UTC)
        available = max(sources[key].available_at for key in rule.source_ids)
        if effective <= at and available <= at:
            candidates.append((effective, rule, available))
    if not candidates:
        raise TiveloError(
            ErrorCode.RULES_UNAVAILABLE, "No known and effective quantity rule for this class"
        )
    _, rule, available = max(candidates, key=lambda item: item[0])
    unlimited = classification.listing_phase in (
        "ipo_first_day",
        "ipo_first_five",
        "relisting_first",
        "delisting_first",
    )
    limit_only = (
        (rule.market_requires_price_limits and unlimited)
        or (rule.risk_warning_limit_only and classification.risk_status == "risk_warning")
        or (
            rule.delisting_limit_only
            and classification.listing_phase in ("delisting_first", "delisting")
        )
    )
    return QuantitySelection(
        preset_version_id=preset.version_id,
        rule_id=rule.rule_id,
        spec_revision=classification.spec_revision,
        effective_from=rule.effective_from,
        available_at=available,
        source_ids=rule.source_ids,
        clauses=rule.clauses,
        order_quantities=tuple(
            q for q in rule.order_quantities if not limit_only or q.order_type == "limit"
        ),
    )
