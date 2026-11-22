"""Pinned official rates compiled with an explicit per-fill research billing policy.

The catalog supplies rates, sides and historical evidence. Broker commission,
inclusion, minima and rounding are separate inputs. Calculation remains in the
existing CNTradingRules implementation; this is not a broker settlement service.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Context, Decimal, localcontext
from importlib import resources
from typing import Literal, Self

from pydantic import Field, TypeAdapter, model_validator
from tivelo_contracts.base import (
    CalendarDate,
    Contract,
    Digest,
    Key,
    NonNegativeDecimal,
    PositiveDecimal,
    fingerprint,
)
from tivelo_contracts.data import HistoricalRecord
from tivelo_contracts.errors import ErrorCode, TiveloError

from .calendar import CN_TIMEZONE
from .presets import CalendarSource, Venue
from .rules import FeeRule

OfficialFee = Literal["handling", "regulatory", "stamp", "transfer"]
ChargeId = Literal["commission", "handling", "regulatory", "stamp", "transfer"]
Rounding = Literal["half_up", "half_even", "up", "down"]
_OFFICIAL = ("handling", "regulatory", "stamp", "transfer")
_DATE = TypeAdapter(CalendarDate)
_VENUE = TypeAdapter(Venue)
_BUILTIN_DIGESTS = {
    "2026.1.0": "39cda44c3190849356ebf7602594d900d186399aee59cecbdd36038e2b828eae",
    "2026.2.0": "45eccf6e4c3adb30d5d9bbefb54b942eb90db5c38788eb1480811df7a28f1741",
}


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), CN_TIMEZONE).astimezone(UTC)


class FeeRate(Contract):
    rate_id: Key
    fee_id: OfficialFee
    venue_ids: tuple[Venue, ...] = Field(min_length=1)
    effective_from: CalendarDate
    date_basis: Literal["official_effective_date", "documented_in_force"]
    side: Literal["buy", "sell", "both"]
    rate: NonNegativeDecimal
    source_ids: tuple[Key, ...] = Field(min_length=1)
    clauses: tuple[Key, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        for items in (self.venue_ids, self.source_ids, self.clauses):
            if items != tuple(sorted(set(items))):
                raise ValueError("fee venues, sources and clauses must be unique and sorted")
        return self


class FeePresetContents(Contract):
    schema_version: Literal["1.0"] = "1.0"
    preset_id: Literal["cn.equity.auction-fees"] = "cn.equity.auction-fees"
    preset_version: Key
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    scope: Literal["domestic_common_stock_auction_rates"] = "domestic_common_stock_auction_rates"
    coverage_start: CalendarDate
    coverage_end: CalendarDate
    verified_on: CalendarDate
    sources: tuple[CalendarSource, ...] = Field(min_length=1)
    corroborating_source_ids: tuple[Key, ...] = ()
    rates: tuple[FeeRate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.coverage_end < self.coverage_start:
            raise ValueError("fee coverage is inclusive and ordered")
        source_ids = tuple(source.source_id for source in self.sources)
        if source_ids != tuple(sorted(set(source_ids))):
            raise ValueError("fee source IDs must be unique and sorted")
        if self.corroborating_source_ids != tuple(sorted(set(self.corroborating_source_ids))):
            raise ValueError("corroborating sources must be unique and sorted")
        referenced = set(self.corroborating_source_ids)
        epochs = []
        for rule in self.rates:
            referenced.update(rule.source_ids)
            epochs.extend((venue, rule.fee_id, rule.effective_from) for venue in rule.venue_ids)
            if rule.effective_from > self.coverage_end:
                raise ValueError("fee epoch starts after coverage")
        if referenced - set(source_ids):
            raise ValueError("fee source reference is missing")
        if len(set(epochs)) != len(epochs):
            raise ValueError("fee epochs must be unique for each venue and fee")
        if len({rule.rate_id for rule in self.rates}) != len(self.rates):
            raise ValueError("fee rate IDs must be unique")
        return self


class FeePreset(Contract):
    preset_format: Literal["tivelo-cn-fee-preset-v1"] = "tivelo-cn-fee-preset-v1"
    version_id: Digest
    contents: FeePresetContents

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.version_id != fingerprint(self.contents):
            raise ValueError("fee preset content digest does not match")
        return self

    @classmethod
    def create(cls, contents: FeePresetContents) -> Self:
        contents = FeePresetContents.model_validate_json(contents.model_dump_json())
        return cls(version_id=fingerprint(contents), contents=contents)


class FeeRounding(Contract):
    fee_id: ChargeId
    quantum: PositiveDecimal
    rounding: Rounding


class BrokerFeePolicy(HistoricalRecord):
    policy_id: Key
    policy_version: Key
    account_scope: Literal["domestic_cash_standard"]
    aggregation: Literal["per_fill_research"]
    commission_rate: NonNegativeDecimal
    commission_minimum: NonNegativeDecimal
    commission_includes: tuple[Literal["handling", "regulatory", "transfer"], ...]
    rounding: tuple[FeeRounding, ...] = Field(min_length=1)
    source_reference: Key

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.commission_includes != tuple(sorted(set(self.commission_includes))):
            raise ValueError("included fees must be unique and sorted")
        required = tuple(sorted({"commission", *_OFFICIAL} - set(self.commission_includes)))
        if tuple(item.fee_id for item in self.rounding) != required:
            raise ValueError("rounding must explicitly cover each separately charged fee once")
        commission = next(item for item in self.rounding if item.fee_id == "commission")
        with localcontext(Context(prec=96)):
            if self.commission_minimum % commission.quantum:
                raise ValueError("commission minimum must align with its charge quantum")
        return self


class FeeCompilationInput(Contract):
    preset_version: Key
    preset_version_id: Digest
    venue_id: Venue
    start: CalendarDate
    end: CalendarDate
    policy: BrokerFeePolicy


class CompiledFees(Contract):
    inputs: FeeCompilationInput
    selected_rate_ids: tuple[Key, ...]
    required_fee_ids: tuple[ChargeId, ...]
    fee_rules: tuple[FeeRule, ...]

    @property
    def version_id(self) -> str:
        return fingerprint(self)


class _RuleIdentity(Contract):
    compilation_input_id: Digest
    rate_id: Key


def load_fee_preset(*, version: str, expected_sha256: str | None = None) -> FeePreset:
    """Load an explicit release; neither network access nor a mutable latest alias is used."""
    expected = _BUILTIN_DIGESTS.get(version)
    if expected is None:
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Unknown CN fee preset version")
    if expected_sha256 is not None and expected_sha256 != expected:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Fee preset differs from the pinned digest")
    try:
        payload = (
            resources.files("tivelo_market_cn")
            .joinpath("fee_presets", f"{version}.json")
            .read_bytes()
        )
        preset = FeePreset.model_validate_json(payload)
    except (OSError, ValueError) as exc:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Installed fee preset is missing or invalid"
        ) from exc
    if preset.version_id != expected or preset.contents.preset_version != version:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Installed fee preset was replaced")
    return preset


def compile_fee_rules(
    preset: FeePreset,
    policy: BrokerFeePolicy,
    *,
    venue_id: str,
    start: date,
    end: date,
) -> CompiledFees:
    """Compile an inclusive date range to existing versioned, per-fill FeeRules.

    One policy must cover the requested range. Changing broker contracts requires
    separately compiled policy intervals; this function never invents a rate,
    inclusion choice or billing aggregation. Returned rules expire at the bound.
    """
    preset = FeePreset.model_validate_json(preset.model_dump_json())
    policy = BrokerFeePolicy.model_validate_json(policy.model_dump_json())
    venue = _VENUE.validate_python(venue_id)
    start, end = _DATE.validate_python(start), _DATE.validate_python(end)
    contents = preset.contents
    if not contents.coverage_start <= start <= end <= contents.coverage_end:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Range is outside fee preset coverage"
        )
    first, stop = _midnight(start), _midnight(end + timedelta(days=1))
    if policy.valid_from > first or (policy.valid_to is not None and policy.valid_to < stop):
        raise TiveloError(
            ErrorCode.RULES_UNAVAILABLE, "Billing policy does not cover the fee range"
        )
    inputs = FeeCompilationInput(
        preset_version=contents.preset_version,
        preset_version_id=preset.version_id,
        venue_id=venue,
        start=start,
        end=end,
        policy=policy,
    )
    context_id = fingerprint(inputs)
    reference = f"cn.fees.research-per-fill:{context_id}"
    rounding = {item.fee_id: item for item in policy.rounding}
    sources = {item.source_id: item for item in contents.sources}
    selected_rates = []
    rules = []

    def add(fee_id, rate_id, rate, side, valid_from, available_at, minimum=Decimal(0), includes=()):
        charge = rounding[fee_id]
        rules.append(
            FeeRule(
                fee_id=fee_id,
                side=side,
                rate=rate,
                minimum=minimum,
                quantum=charge.quantum,
                rounding=charge.rounding,
                included_fee_ids=includes,
                source_reference=reference,
                valid_from=valid_from,
                valid_to=stop,
                available_at=max(available_at, policy.available_at),
                revision_id=fingerprint(
                    _RuleIdentity(compilation_input_id=context_id, rate_id=rate_id)
                ),
                source_id=contents.preset_id,
            )
        )

    add(
        "commission",
        "broker-commission",
        policy.commission_rate,
        "both",
        first,
        policy.available_at,
        policy.commission_minimum,
        policy.commission_includes,
    )
    for fee_id in _OFFICIAL:
        history = sorted(
            (item for item in contents.rates if item.fee_id == fee_id and venue in item.venue_ids),
            key=lambda item: item.effective_from,
        )
        prior = [item for item in history if item.effective_from <= start]
        if not prior:
            raise TiveloError(ErrorCode.RULES_UNAVAILABLE, f"No initial official rate for {fee_id}")
        initial = prior[-1]
        if max(sources[key].available_at for key in initial.source_ids) > first:
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE, f"Initial {fee_id} rate was not yet known"
            )
        selected = [initial, *(item for item in history if start < item.effective_from <= end)]
        selected_rates.extend(item.rate_id for item in selected)
        if fee_id in policy.commission_includes:
            continue
        for item in selected:
            add(
                fee_id,
                item.rate_id,
                item.rate,
                item.side,
                max(first, _midnight(item.effective_from)),
                max(sources[key].available_at for key in item.source_ids),
            )
    return CompiledFees(
        inputs=inputs,
        selected_rate_ids=tuple(sorted(selected_rates)),
        required_fee_ids=tuple(sorted(rounding)),
        fee_rules=tuple(
            sorted(rules, key=lambda item: (item.fee_id, item.valid_from, item.available_at))
        ),
    )
