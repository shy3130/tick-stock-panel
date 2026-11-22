"""Offline daily price limits; order-entry price cages are a separate constraint."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from importlib import resources
from typing import Literal, Self

from pydantic import Field, model_validator
from tivelo_contracts.base import (
    CalendarDate,
    Contract,
    Digest,
    Key,
    PositiveDecimal,
    UTCDateTime,
    fingerprint,
    utc_datetime,
)
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import TradingTerms

from .calendar import CN_TIMEZONE
from .classification import Board, CNEquityClass
from .presets import CalendarSource, Venue

_BUILTIN_DIGESTS = {
    "2026.1.0": "cbd72ad76b27558322e58770e46921c6c5215c482e052db85ddb7cae4dd3fede",
    "2026.2.0": "a68c9311cdd9434ff7024e4bd8ead0741a847bfc943efb46e57a48bc3a8304bc",
}
UnlimitedPhase = Literal["ipo_first_day", "ipo_first_five", "relisting_first", "delisting_first"]


def _midnight(day) -> datetime:
    return datetime.combine(day, time(), CN_TIMEZONE).astimezone(UTC)


class PriceSchedule(Contract):
    rule_id: Key
    venue_id: Venue
    board: Board
    effective_from: CalendarDate
    price_tick: PositiveDecimal
    regular_ratio: PositiveDecimal = Field(le=1)
    risk_warning_ratio: PositiveDecimal = Field(le=1)
    delisting_ratio: PositiveDecimal = Field(le=1)
    unlimited_phases: tuple[UnlimitedPhase, ...]
    source_ids: tuple[Key, ...] = Field(min_length=1)
    clauses: tuple[Key, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        CNEquityClass(
            venue_id=self.venue_id, board=self.board, risk_status="normal", listing_phase="regular"
        )
        for values in (self.source_ids, self.clauses, self.unlimited_phases):
            if len(values) != len(set(values)):
                raise ValueError("price schedule references and phases must be unique")
        return self


class PricePresetContents(Contract):
    schema_version: Literal["1.0"] = "1.0"
    preset_id: Literal["cn.equity.daily-price-limits"] = "cn.equity.daily-price-limits"
    preset_version: Key
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    scope: Literal["daily_price_limits_and_tick"] = "daily_price_limits_and_tick"
    coverage_start: CalendarDate
    coverage_end: CalendarDate
    verified_on: CalendarDate
    sources: tuple[CalendarSource, ...] = Field(min_length=1)
    schedules: tuple[PriceSchedule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.coverage_end < self.coverage_start:
            raise ValueError("price coverage must be ordered")
        sources = {source.source_id for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("price source IDs must be unique")
        if len({rule.rule_id for rule in self.schedules}) != len(self.schedules):
            raise ValueError("price rule IDs must be unique")
        if len({(r.venue_id, r.board, r.effective_from) for r in self.schedules}) != len(
            self.schedules
        ):
            raise ValueError("price rule epochs must be unique within a board")
        for rule in self.schedules:
            if set(rule.source_ids) - sources:
                raise ValueError("price rule source is missing")
            if rule.effective_from > self.coverage_end:
                raise ValueError("price rule begins outside coverage")
        return self


class PricePreset(Contract):
    preset_format: Literal["tivelo-cn-price-preset-v1"] = "tivelo-cn-price-preset-v1"
    version_id: Digest
    contents: PricePresetContents

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.version_id != fingerprint(self.contents):
            raise ValueError("price preset content digest does not match")
        return self

    @classmethod
    def create(cls, contents: PricePresetContents) -> Self:
        contents = PricePresetContents.model_validate_json(contents.model_dump_json())
        return cls(version_id=fingerprint(contents), contents=contents)


class PriceSelection(Contract):
    scope: Literal["daily_price_limits_and_tick"] = "daily_price_limits_and_tick"
    preset_version_id: Digest
    rule_id: Key
    spec_revision: Key
    effective_from: CalendarDate
    available_at: UTCDateTime
    source_ids: tuple[Key, ...]
    clauses: tuple[Key, ...]
    price_tick: PositiveDecimal
    price_limit_mode: Literal["relative_reference", "unlimited"]
    limit_up_ratio: PositiveDecimal | None
    limit_down_ratio: PositiveDecimal | None


class PriceCompilationInput(Contract):
    preset_version: Key
    preset_version_id: Digest
    classification: CNEquityClass
    base_terms: tuple[TradingTerms, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        identity = self.classification
        for term in self.base_terms:
            if (
                term.spec_revision != identity.spec_revision
                or term.market_id != "cn.equity"
                or term.venue_id != identity.venue_id
                or term.asset_class != "equity"
                or term.instrument_type != identity.instrument_type
                or term.quantity_unit != "share"
                or term.currency != "CNY"
            ):
                raise ValueError("base terms must match the explicit CN equity class")
            if term.valid_to is None:
                raise ValueError("price compilation requires bounded base term intervals")
        if len({t.revision_id for t in self.base_terms}) != len(self.base_terms):
            raise ValueError("base term revision IDs must be unique")
        if len({(t.valid_from, t.available_at) for t in self.base_terms}) != len(self.base_terms):
            raise ValueError("base terms have ambiguous effective and knowledge epochs")
        return self


class CompiledPriceTerms(Contract):
    inputs: PriceCompilationInput
    selected_rule_ids: tuple[Key, ...] = Field(min_length=1)
    terms: tuple[TradingTerms, ...] = Field(min_length=1)

    @property
    def version_id(self) -> str:
        return fingerprint(self)


class _PriceRevision(Contract):
    input_id: Digest
    base_revision: Key
    rule_id: Key
    valid_from: UTCDateTime
    valid_to: UTCDateTime


def load_price_preset(*, version: str, expected_sha256: str | None = None) -> PricePreset:
    """Load a pinned installed release, without resolving latest or accessing a network."""
    expected = _BUILTIN_DIGESTS.get(version)
    if expected is None:
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Unknown CN price preset version")
    if expected_sha256 is not None and expected_sha256 != expected:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Price preset differs from the pinned digest")
    try:
        payload = (
            resources.files("tivelo_market_cn")
            .joinpath("price_presets", f"{version}.json")
            .read_bytes()
        )
        preset = PricePreset.model_validate_json(payload)
    except (OSError, ValueError) as exc:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Installed price preset is missing or invalid"
        ) from exc
    if preset.version_id != expected or preset.contents.preset_version != version:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Installed price preset was replaced")
    return preset


def _schedules(preset: PricePreset, classification: CNEquityClass) -> tuple[PriceSchedule, ...]:
    if classification.board == "star" and classification.listing_phase == "relisting_first":
        raise TiveloError(ErrorCode.UNSUPPORTED_ASSET, "STAR relisting price policy is not covered")
    return tuple(
        sorted(
            (
                rule
                for rule in preset.contents.schedules
                if (rule.venue_id, rule.board) == (classification.venue_id, classification.board)
            ),
            key=lambda rule: rule.effective_from,
        )
    )


def _select(preset: PricePreset, classification: CNEquityClass, rule: PriceSchedule):
    phase = classification.listing_phase
    if phase in rule.unlimited_phases:
        ratio = None
    elif phase == "delisting":
        ratio = rule.delisting_ratio
    elif phase != "regular":
        raise TiveloError(ErrorCode.UNSUPPORTED_ASSET, "Price policy does not cover this phase")
    elif classification.risk_status == "risk_warning":
        ratio = rule.risk_warning_ratio
    else:
        ratio = rule.regular_ratio
    sources = {source.source_id: source for source in preset.contents.sources}
    return PriceSelection(
        preset_version_id=preset.version_id,
        rule_id=rule.rule_id,
        spec_revision=classification.spec_revision,
        effective_from=rule.effective_from,
        available_at=max(sources[key].available_at for key in rule.source_ids),
        source_ids=rule.source_ids,
        clauses=rule.clauses,
        price_tick=rule.price_tick,
        price_limit_mode="unlimited" if ratio is None else "relative_reference",
        limit_up_ratio=ratio,
        limit_down_ratio=ratio,
    )


def resolve_price_rules(
    preset: PricePreset, classification: CNEquityClass, *, at: datetime
) -> PriceSelection:
    """Resolve daily bounds only, requiring the effective epoch to be known at this instant."""
    at = utc_datetime(at)
    preset = PricePreset.model_validate_json(preset.model_dump_json())
    classification = CNEquityClass.model_validate_json(classification.model_dump_json())
    day = at.astimezone(CN_TIMEZONE).date()
    if not preset.contents.coverage_start <= day <= preset.contents.coverage_end:
        raise TiveloError(ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Date is outside price coverage")
    candidates = [r for r in _schedules(preset, classification) if r.effective_from <= day]
    if not candidates:
        raise TiveloError(ErrorCode.RULES_UNAVAILABLE, "No effective price rule for this class")
    selected = _select(preset, classification, candidates[-1])
    if selected.available_at > at:
        raise TiveloError(ErrorCode.RULES_UNAVAILABLE, "Effective price rule was not yet known")
    return selected


def compile_price_terms(
    preset: PricePreset,
    classification: CNEquityClass,
    base_terms: tuple[TradingTerms, ...],
) -> CompiledPriceTerms:
    """Replace only price fields, splitting bounded historical terms at official rule epochs.

    Quantity, sell lag and their original provenance remain explicit archived
    inputs. This compilation neither establishes account permission nor infers
    a security's classification or the duration of its listing phase.
    """
    preset = PricePreset.model_validate_json(preset.model_dump_json())
    inputs = PriceCompilationInput.model_validate_json(
        PriceCompilationInput(
            preset_version=preset.contents.preset_version,
            preset_version_id=preset.version_id,
            classification=classification,
            base_terms=base_terms,
        ).model_dump_json()
    )
    schedules = _schedules(preset, inputs.classification)
    coverage_start = _midnight(preset.contents.coverage_start)
    coverage_end = _midnight(preset.contents.coverage_end + timedelta(days=1))
    input_id = fingerprint(inputs)
    terms = []
    selected_ids = set()
    for base in inputs.base_terms:
        assert base.valid_to is not None
        if base.valid_from < coverage_start or base.valid_to > coverage_end:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Base terms exceed price preset coverage"
            )
        if not schedules or _midnight(schedules[0].effective_from) > base.valid_from:
            raise TiveloError(ErrorCode.RULES_UNAVAILABLE, "Initial price rule is unavailable")
        for i, rule in enumerate(schedules):
            begin = max(base.valid_from, _midnight(rule.effective_from))
            end = min(
                base.valid_to,
                _midnight(schedules[i + 1].effective_from)
                if i + 1 < len(schedules)
                else coverage_end,
            )
            if end <= begin:
                continue
            selected = _select(preset, inputs.classification, rule)
            available = max(base.available_at, selected.available_at)
            first_eligible = max(begin, available)
            # A price epoch can collapse distinct base effective dates into
            # the same compiled date. Remove already superseded bases before
            # that collapse, including late corrections of an older epoch.
            # Expiry of the newer base does not revive the older one.
            if any(
                (other.valid_from, other.available_at) > (base.valid_from, base.available_at)
                and other.valid_from <= first_eligible
                and other.available_at <= first_eligible
                for other in inputs.base_terms
            ):
                continue
            selected_ids.add(rule.rule_id)
            revision = fingerprint(
                _PriceRevision(
                    input_id=input_id,
                    base_revision=base.revision_id,
                    rule_id=rule.rule_id,
                    valid_from=begin,
                    valid_to=end,
                )
            )
            terms.append(
                TradingTerms.model_validate(
                    base.model_dump()
                    | {
                        "valid_from": begin,
                        "valid_to": end,
                        "available_at": available,
                        "revision_id": f"cn.prices.{revision}",
                        "source_id": "tivelo-market-cn",
                        "source_reference": f"cn.prices:{input_id}",
                        "price_tick": selected.price_tick,
                        "price_limit_mode": selected.price_limit_mode,
                        "limit_up_ratio": selected.limit_up_ratio,
                        "limit_down_ratio": selected.limit_down_ratio,
                    }
                )
            )
    return CompiledPriceTerms(
        inputs=inputs,
        selected_rule_ids=tuple(sorted(selected_ids)),
        terms=tuple(sorted(terms, key=lambda t: (t.valid_from, t.available_at, t.revision_id))),
    )
