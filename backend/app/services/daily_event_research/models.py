"""Frozen contracts for Issue #45 auditable daily-event research.

The module studies one pre-registered daily detector over per-symbol ordered
daily-bar slices read from the generation-pinned sealed canonical.  Requests
never touch ``current`` data, responses never promote anything, and research
insufficiency is always an explicit ``unavailable`` instead of a degraded
"pass".  Detectors are pure: they receive bars and a calendar and perform no
I/O; only :mod:`.evaluation` binds the pinned reader seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Literal, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.hold_firm_patterns.models import SYMBOL_PATTERN

DEFINITION_VERSION = "v1"
DEFINITION_DOCUMENT = "docs/ISSUE-45/final-design.md"
RESPONSE_SCHEMA = "daily_event_research/v1"
HORIZON_DAYS_DEFAULT = 20
HORIZON_DAYS_MAX = 60
COST_BPS_DEFAULT = 10.0
COST_BPS_MAX = 1000.0
SYMBOLS_MAX = 200
DUGU_ALIGNMENT_DAY_CHOICES = (10, 30, 50, 100)
DUGU_ALIGNMENT_DAYS_DEFAULT = 30


class DailyEventStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class DailyEventVerdict(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class UnavailabilityReason(str, Enum):
    CANONICAL_READER = "unavailable_canonical_reader"
    NO_EVENTS = "unavailable_no_qualified_events"
    MARKET_FACTS = "unavailable_market_facts"


class CensorReason(str, Enum):
    WARMUP_INCOMPLETE = "censor_warmup_incomplete"
    HORIZON_INCOMPLETE = "censor_horizon_incomplete"
    ENTRY_INCOMPLETE = "censor_entry_incomplete"
    ENTRY_LIMIT_UP_BLOCKED = "censor_entry_limit_up_blocked"
    EXIT_LIMIT_DOWN_BLOCKED = "censor_exit_limit_down_blocked"
    MARKET_FACTS_MISSING = "censor_market_facts_missing"


BandMode = Literal["fixed", "atr"]
DuguVariantId = Literal["ma_24_72", "ma_20_70"]
DetectorId = Literal["dugu_trend"]


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        populate_by_name=True,
        serialize_by_alias=True,
    )


@dataclass(frozen=True, slots=True)
class DetectionEvidence:
    qualified: bool
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Detection:
    """One deterministic detector verdict for a single signal day.

    ``evidence`` xor ``censor``: a censored detection records why the
    pre-registered windows could not be evaluated; an evidenced detection is a
    counted parent event whose ``qualified`` flag decides the analysis bucket.
    """

    detector_id: str
    variant: str
    symbol: str
    signal_date: date
    evidence: DetectionEvidence | None = None
    censor: CensorReason | None = None

    def __post_init__(self) -> None:
        if (self.evidence is None) == (self.censor is None):
            raise ValueError("detection requires evidence xor censor")


@runtime_checkable
class DailyEventDetector(Protocol):
    @property
    def detector_id(self) -> str: ...

    @property
    def variant(self) -> str: ...

    def detect(
        self, symbol: str, bars: Sequence[object], calendar: Sequence[date]
    ) -> tuple[Detection, ...]: ...


class DailyEventRequest(_Strict):
    detector_id: DetectorId = "dugu_trend"
    variant: DuguVariantId
    band_mode: BandMode = "fixed"
    require_m3: bool = False
    alignment_days: int = DUGU_ALIGNMENT_DAYS_DEFAULT
    symbols: list[str] = Field(min_length=1, max_length=SYMBOLS_MAX)
    start: date
    oos_start: date
    end: date
    horizon_days: int = Field(default=HORIZON_DAYS_DEFAULT, ge=1, le=HORIZON_DAYS_MAX)
    cost_bps: float = Field(default=COST_BPS_DEFAULT, ge=0, le=COST_BPS_MAX)

    @field_validator("alignment_days")
    @classmethod
    def validate_alignment_days(cls, value: int) -> int:
        if value not in DUGU_ALIGNMENT_DAY_CHOICES:
            raise ValueError("alignment_days must be one of the frozen scan values")
        return value

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
    def dates(self) -> DailyEventRequest:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class DailyEventIdentity(_Strict):
    """Pinned canonical identity plus its publication stamp.

    ``available_at`` is the sealed generation stamp itself; the pinned reader
    never follows ``current`` after construction, so this is the exact moment
    the studied data became available.  Nothing is inferred beyond it.
    """

    canonical: "CanonicalIdentityRef"
    available_at: str = Field(min_length=1)

    market_facts: dict[str, str]


class CanonicalIdentityRef(_Strict):
    """Structural echo of the pinned sealed-canonical identity."""

    generation: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_generations: dict[str, str]
    calendar_id: str = Field(min_length=1)


class EventCensor(_Strict):
    detector_id: str
    variant: str
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    signal_date: date
    reason: CensorReason
    detail: str = ""


class EventOutcome(_Strict):
    event_id: str
    detector_id: str
    variant: str
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    signal_date: date
    qualified: bool
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    raw_forward_return: float
    cost_adjusted_forward_return: float
    oos: bool


class DailyEventCoverage(_Strict):
    symbols_requested: int = Field(ge=0)
    symbols_with_bars: int = Field(ge=0)
    bar_rows: int = Field(ge=0)
    parent_events: int = Field(ge=0)
    qualified_events: int = Field(ge=0)
    not_selected_events: int = Field(ge=0)
    censored_detections: int = Field(ge=0)
    horizon_incomplete: int = Field(ge=0)

    @model_validator(mode="after")
    def partition(self) -> DailyEventCoverage:
        if self.parent_events != self.qualified_events + self.not_selected_events:
            raise ValueError("parent events must partition into qualified/not_selected")
        return self


class DailyEventVerdicts(_Strict):
    verdict: DailyEventVerdict
    in_sample: dict[str, object] = Field(default_factory=dict)
    oos: dict[str, object] = Field(default_factory=dict)


class DailyEventProvenance(_Strict):
    definition_version: Literal["v1"] = "v1"
    code_version: str
    parameters: dict[str, object]
    params_provenance: dict[str, str]
    source_evidence_paths: tuple[str, ...] = (DEFINITION_DOCUMENT,)


class DailyEventResponse(_Strict):
    schema_: Literal["daily_event_research/v1"] = Field(
        default=RESPONSE_SCHEMA,
        alias="schema",
    )
    status: DailyEventStatus
    definition_version: Literal["v1"] = "v1"
    request: DailyEventRequest
    identity: DailyEventIdentity | None = None
    coverage: DailyEventCoverage | None = None
    censored: list[EventCensor] = Field(default_factory=list)
    events: list[EventOutcome] = Field(default_factory=list)
    verdicts: DailyEventVerdicts | None = None
    provenance: DailyEventProvenance | None = None
    promoted: Literal[False] = False
    unavailable_reason: UnavailabilityReason | None = None

    @model_validator(mode="after")
    def envelope(self) -> DailyEventResponse:
        if self.status is DailyEventStatus.UNAVAILABLE:
            if self.events or self.unavailable_reason is None:
                raise ValueError("unavailable response must be empty and carry a reason")
            if any(
                value is not None
                for value in (self.identity, self.coverage, self.verdicts, self.provenance)
            ):
                raise ValueError("unavailable response must not carry research payloads")
        else:
            if self.unavailable_reason is not None:
                raise ValueError("ok response must not carry unavailable_reason")
            if any(
                value is None
                for value in (self.identity, self.coverage, self.verdicts, self.provenance)
            ):
                raise ValueError("ok response requires identity, coverage, verdicts, provenance")
        return self


def unavailable_response(
    request: DailyEventRequest, reason: UnavailabilityReason
) -> DailyEventResponse:
    """Fail closed: an empty, explicit, JSON-serializable envelope."""
    return DailyEventResponse(
        status=DailyEventStatus.UNAVAILABLE,
        request=request,
        unavailable_reason=reason,
    )


def validate_event_partition(parent: int, qualified: int, not_selected: int, censored: int) -> None:
    if parent != qualified + not_selected:
        raise ValueError("parent count invariant violated")
    if parent + censored < parent:
        raise ValueError("censored count overflow")
