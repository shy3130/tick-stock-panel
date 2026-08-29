"""Frozen shared contract for Issue #38 hold-firm-pattern research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Literal, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFINITION_VERSION = "v1"
DEFINITION_DOCUMENT = "docs/ISSUE-38/final-design.md"
FACTOR_IDS = (
    "first_yin_complement",
    "breakout_pullback",
    "low_gentle_slope",
    "bottom_platform_breakout",
)
FactorId = Literal[
    "first_yin_complement", "breakout_pullback", "low_gentle_slope", "bottom_platform_breakout"
]
HORIZON_DAYS, COST_BPS_DEFAULT, COST_BPS_MAX = 20, 10, 1000
FORWARD_CHECKPOINT_DAYS = (1, 5, 10, 20)
MIN_OOS_EVENTS, MIN_OOS_SYMBOLS = 30, 10
BOOTSTRAP_SEED, BOOTSTRAP_ROUNDS, MIN_VALID_BOOTSTRAP_REPLICATES = 42, 5000, 4750
CI_LEVEL, CI_LOWER_QUANTILE, CI_UPPER_QUANTILE = 0.95, 0.025, 0.975
PRICE_ABS_TOL = 0.005
REQUIRED_CANONICAL_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "volume",
    "amount",
)
SYMBOL_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"


class HoldFirmStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class HoldFirmVerdict(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class UnavailabilityReason(str, Enum):
    CANONICAL_READER = "unavailable_canonical_reader"
    MARKET_FACTS_INCOMPLETE = "unavailable_market_facts_incomplete"
    UNIVERSE_PRESENCE = "unavailable_universe_presence"
    INVALID_PROVENANCE = "unavailable_invalid_provenance"
    BOOTSTRAP = "unavailable_bootstrap_min_valid_replicates"


class SelectionBucket(str, Enum):
    QUALIFIED = "qualified"
    NOT_SELECTED = "not_selected"


class HoldingArm(str, Enum):
    DYNAMIC_DEFENSE = "dynamic_defense"
    FIXED_HOLD_20D = "fixed_hold_20d"


class LandmarkKind(str, Enum):
    FIRST_YIN_NEXT_CLOSE = "first_yin_next_close"
    BREAKOUT_DAY5_CLOSE = "breakout_day5_close"
    SIGNAL_DAY_CLOSE = "signal_day_close"


class PitUniverseStatus(str, Enum):
    IN_POOL = "in_pool"
    NOT_IN_POOL = "not_in_pool"
    COVERAGE_MISSING = "coverage_missing"


class CensorReason(str, Enum):
    SELECTION_WINDOW_INCOMPLETE = "censor_selection_window_incomplete"
    LOW_POSITION_UNDEFINED = "censor_low_position_undefined"
    WARMUP_INCOMPLETE = "censor_warmup_incomplete"
    HORIZON_INCOMPLETE = "censor_horizon_incomplete"
    ENTRY_BAR_MISSING = "censor_entry_bar_missing"
    ENTRY_OPEN_INVALID = "censor_entry_open_invalid"
    ENTRY_UNREACHABLE = "censor_entry_unreachable"
    EXIT_UNREACHABLE = "censor_exit_unreachable"
    PENDING_EXIT = "realization_censor_pending_exit"
    EVENT_OVERLAP = "censor_same_factor_symbol_overlap"
    DIAGNOSTIC_WINDOW_INCOMPLETE = "censor_diagnostic_window_incomplete"


class DenominatorAuditCode(str, Enum):
    PIT_UNIVERSE_INELIGIBLE = "pit_universe_ineligible"


SELECTION_STAGE_CENSOR_REASONS = frozenset(
    (CensorReason.SELECTION_WINDOW_INCOMPLETE, CensorReason.LOW_POSITION_UNDEFINED)
)


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    date: date
    research_open_adj: float
    research_high_adj: float
    research_low_adj: float
    research_close_adj: float
    quote_open_raw: float
    quote_high_raw: float
    quote_low_raw: float
    quote_close_raw: float
    volume: float
    amount: float


@dataclass(frozen=True, slots=True)
class MarketFactsRow:
    symbol: str
    date: date
    quote_open_raw: float
    quote_high_raw: float
    quote_low_raw: float
    quote_close_raw: float
    pre_close: float
    published_limit_up: float
    published_limit_down: float
    regime: str | None
    is_st: bool
    name: str


@dataclass(frozen=True, slots=True)
class Landmark:
    kind: LandmarkKind
    anchor_date: date
    landmark_date: date


@dataclass(frozen=True, slots=True)
class DetectionEvidence:
    qualified: bool
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ParentDetection:
    factor_id: FactorId
    symbol: str
    anchor_date: date
    landmark: Landmark | None
    evidence: DetectionEvidence | None = None
    censor: CensorReason | None = None

    def __post_init__(self):
        if (self.evidence is None) == (self.censor is None):
            raise ValueError("detection requires evidence xor censor")


@dataclass(frozen=True, slots=True)
class ParentEvent:
    factor_id: FactorId
    event_id: str
    symbol: str
    anchor_date: date
    bucket: SelectionBucket | None
    pit_status: PitUniverseStatus
    censor: CensorReason | None = None
    audit_code: DenominatorAuditCode | None = None

    def __post_init__(self):
        if self.pit_status is PitUniverseStatus.COVERAGE_MISSING:
            raise ValueError("missing universe coverage is order-level unavailable")


@dataclass(frozen=True, slots=True)
class EventGroup:
    factor_id: FactorId
    qualified: tuple[ParentEvent, ...]
    not_selected: tuple[ParentEvent, ...]
    pit_ineligible: tuple[ParentEvent, ...]
    selection_window_censored: tuple[ParentEvent, ...]


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, populate_by_name=True
    )


class CanonicalIdentity(_Strict):
    generation: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_generations: dict[str, str]
    calendar_id: str = Field(min_length=1)


class MarketFactsIdentity(_Strict):
    generation: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UniverseDayIdentity(_Strict):
    """Exact-day presence content identity used for one membership day."""

    day: date
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class UniverseIdentity(_Strict):
    generation: str = Field(pattern=r"^\d{8}T\d{6}Z-[0-9a-f]{16}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal[2]
    artifact: Literal["universe_presence"]
    rule_version: Literal["presence_v1"]
    retrospective: Literal[True]
    status_filter: Literal["daily_market_row_present_exact_day"]
    source_artifact: Literal["fstore_snapshot"]
    source_generation: str = Field(pattern=r"^\d{8}T\d{6}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    day_identities: tuple[UniverseDayIdentity, ...]

    @field_validator("day_identities")
    @classmethod
    def unique_sorted_days(
        cls, value: tuple[UniverseDayIdentity, ...]
    ) -> tuple[UniverseDayIdentity, ...]:
        days = [item.day for item in value]
        if days != sorted(set(days)):
            raise ValueError("day_identities must be unique and sorted by day")
        return value


class DataIdentity(_Strict):
    canonical: CanonicalIdentity
    markets: MarketFactsIdentity
    universe: UniverseIdentity


class HoldFirmPatternsRequest(_Strict):
    symbols: list[str] = Field(min_length=1, max_length=200)
    start: date
    oos_start: date
    end: date
    cost_bps: float = Field(default=COST_BPS_DEFAULT, ge=0, le=COST_BPS_MAX)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, symbols: list[str]) -> list[str]:
        import re

        normalized = [symbol.strip().upper() for symbol in symbols]
        if any(re.fullmatch(SYMBOL_PATTERN, symbol) is None for symbol in normalized):
            raise ValueError("symbols must be canonical A-share identifiers")
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbols must be unique")
        return normalized

    @model_validator(mode="after")
    def dates(self):
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class Censor(_Strict):
    factor_id: FactorId
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    event_date: date | None
    reason: CensorReason
    detail: str = ""


class DenominatorAuditEntry(_Strict):
    event_id: str
    factor_id: FactorId
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    event_date: date
    code: DenominatorAuditCode


class ExecutionSegment(_Strict):
    event_id: str
    factor_id: FactorId
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    arm: HoldingArm
    entry_date: date
    entry_quote_raw: float
    exit_date: date | None = None
    exit_quote_raw: float | None = None
    pending_exit: bool = False
    entry_cost_bps: float = Field(ge=0)
    exit_cost_bps: float | None = Field(default=None, ge=0)
    holding_days: int = Field(ge=1)
    pending_days: int = Field(default=0, ge=0)
    checkpoint_returns: dict[int, float] = Field(default_factory=dict)
    terminal_return: float
    liquidation_cost_adjusted_terminal_return: float
    mae: float
    mfe: float


class FactorResult(_Strict):
    factor_id: FactorId
    parent_events: int = Field(ge=0)
    qualified_events: int = Field(ge=0)
    not_selected_events: int = Field(ge=0)
    segments: list[ExecutionSegment] = Field(default_factory=list)
    censored: list[Censor] = Field(default_factory=list)
    denominator_audit: list[DenominatorAuditEntry] = Field(default_factory=list)
    is_diagnostic: dict[str, object] | None = Field(default=None, alias="is")
    oos: dict[str, object] = Field(default_factory=dict)
    diagnostics: dict[str, object] = Field(default_factory=dict)
    selection_verdict: HoldFirmVerdict
    holding_verdict: HoldFirmVerdict
    verdict: HoldFirmVerdict


class Provenance(_Strict):
    definition_version: Literal["v1"] = "v1"
    identities: DataIdentity
    required_columns: tuple[str, ...] = REQUIRED_CANONICAL_COLUMNS
    calendar_id: str
    parameters: dict[str, object]
    params_provenance: dict[str, str]
    code_version: str
    source_evidence_paths: tuple[str, ...] = (
        DEFINITION_DOCUMENT,
        "obsidian-note/clipper/2026-08-27-qinchuan-four-types-hold-firm.md",
    )


class HoldFirmResponse(_Strict):
    status: HoldFirmStatus
    definition_version: Literal["v1"] = "v1"
    factors: list[FactorResult] = Field(default_factory=list)
    unavailable_reason: UnavailabilityReason | None = None
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def response(self):
        if self.status is HoldFirmStatus.OK and (
            len(self.factors) != 4 or tuple(x.factor_id for x in self.factors) != FACTOR_IDS
        ):
            raise ValueError("ok response requires four ordered factors")
        if self.status is HoldFirmStatus.UNAVAILABLE and (
            self.factors or self.unavailable_reason is None
        ):
            raise ValueError("unavailable response must be empty")
        return self


class CapabilityResult(_Strict):
    status: HoldFirmStatus
    identities: DataIdentity | None = None
    problems: tuple[str, ...] = ()


@runtime_checkable
class CanonicalDailyReader(Protocol):
    def identity(self) -> CanonicalIdentity: ...
    def trading_days(self, start: date, end: date) -> tuple[date, ...]: ...
    def load_bars(self, symbol: str, start: date, end: date) -> tuple[Bar, ...]: ...


@runtime_checkable
class MarketFactsSource(Protocol):
    def identity(self) -> MarketFactsIdentity: ...
    def row(self, symbol: str, day: date) -> MarketFactsRow | None: ...


@runtime_checkable
class UniverseReader(Protocol):
    def identity(self) -> UniverseIdentity: ...
    def membership(self, symbol: str, day: date) -> PitUniverseStatus: ...


@runtime_checkable
class HoldFirmDetector(Protocol):
    @property
    def factor_id(self) -> FactorId: ...
    def detect(
        self, symbol: str, bars: Sequence[Bar], facts: MarketFactsSource, calendar: Sequence[date]
    ) -> tuple[ParentDetection, ...]: ...


def validate_factor_coverage(factors: Sequence[FactorResult]) -> None:
    if len(factors) != 4 or tuple(f.factor_id for f in factors) != FACTOR_IDS:
        raise ValueError("factors must be exactly four ordered IDs")


def validate_group_partition(qualified: Sequence[str], not_selected: Sequence[str]) -> None:
    if set(qualified) & set(not_selected):
        raise ValueError("qualified/not_selected overlap")


def validate_holding_arm_alignment(dynamic: Sequence[str], fixed: Sequence[str]) -> None:
    if set(dynamic) != set(fixed):
        raise ValueError("holding event IDs must align")


def validate_unavailable_response(response: HoldFirmResponse) -> None:
    if (
        response.status is not HoldFirmStatus.UNAVAILABLE
        or response.factors
        or response.unavailable_reason is None
    ):
        raise ValueError("invalid unavailable structure")


def validate_count_invariants(
    parent: int, qualified: int, not_selected: int, pit_ineligible: int, selection_censored: int
) -> None:
    if parent != qualified + not_selected + pit_ineligible + selection_censored:
        raise ValueError("parent count invariant violated")


def combine_verdicts(selection: HoldFirmVerdict, holding: HoldFirmVerdict) -> HoldFirmVerdict:
    if HoldFirmVerdict.REJECTED in (selection, holding):
        return HoldFirmVerdict.REJECTED
    return (
        HoldFirmVerdict.ACCEPTED
        if selection is holding is HoldFirmVerdict.ACCEPTED
        else HoldFirmVerdict.UNAVAILABLE
    )
