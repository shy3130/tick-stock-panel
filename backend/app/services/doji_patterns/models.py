"""Strict contract for the four doji research factors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.hold_firm_patterns.models import (
    COST_BPS_DEFAULT,
    COST_BPS_MAX,
    REQUIRED_CANONICAL_COLUMNS,
    SYMBOL_PATTERN,
    CensorReason,
    DataIdentity,
    DenominatorAuditCode,
    DetectionEvidence,
    PitUniverseStatus,
    SelectionBucket,
    UnavailabilityReason,
)

DEFINITION_VERSION = "v1"
DOJI_FACTOR_IDS = (
    "doji_position_interaction",
    "gravestone_high",
    "t_bar_low",
    "next_day_confirmation",
    "tail_session_doji",
)
DojiFactorId = Literal[
    "doji_position_interaction",
    "gravestone_high",
    "t_bar_low",
    "next_day_confirmation",
    "tail_session_doji",
]
HORIZON_DAYS = 10
FORWARD_CHECKPOINT_DAYS = (1, 5, 10)
DOJI_BODY_RATIO_MAX = 0.10
DOJI_SHADOW_BODY_MULT = 2.0
DEFINITION_DOCUMENT = "docs/TODO.md#评估三种十字星形态反转信号d1-d4-k线形态因子组"
DOJI_POSITION_WINDOW_DAYS = 20
DOJI_POSITION_HIGH_MIN = 0.70
DOJI_POSITION_LOW_MAX = 0.30
DOJI_PRIOR_MOVE_WINDOW_DAYS = 20
DOJI_PRIOR_MOVE_MIN_PCT = 0.10
DOJI_VOLUME_REF_WINDOW = 20
DOJI_VOLUME_SHRINK_MAX = 0.70
DOJI_VOLUME_EXPAND_MIN = 1.50
TAIL_OPEN_ANCHOR_MINUTE_INDEX = 209
TAIL_WINDOW_MINUTE_INDICES = tuple(range(210, 240))
TAIL_BARE_BODY_RATIO_MIN = 0.90
TAIL_VOLUME_SHARE_SHRINK_MAX = 0.10
TAIL_VOLUME_SHARE_EXPAND_MIN = 0.20
TAIL_DIRECTION_FLAT_BAND = 0.001
TAIL_SESSION_SHAPES = ("bare_yang", "bare_yin", "shrinking_doji")
DOJI_OOS_START_DEFAULT = date(2025, 7, 1)
SOURCE_EVIDENCE_PATHS = (
    DEFINITION_DOCUMENT,
    "obsidian-note/clipper/2026-08-29-qinchuan-three-doji-patterns.md",
)


class DojiStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class DojiVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class PositionStratum(StrEnum):
    HIGH = "high"
    LOW = "low"
    MIDDLE = "middle"


class VolumeState(StrEnum):
    SHRINK = "shrink"
    FLAT = "flat"
    EXPAND = "expand"


class ConfirmDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class DojiArm(StrEnum):
    BARE = "bare"
    CONFIRMED = "confirmed"


class DojiLandmarkKind(StrEnum):
    SIGNAL_DAY_CLOSE = "signal_day_close"


@dataclass(frozen=True, slots=True)
class DojiLandmark:
    kind: DojiLandmarkKind
    anchor_date: date
    landmark_date: date


@dataclass(frozen=True, slots=True)
class DojiDetection:
    factor_id: DojiFactorId
    symbol: str
    anchor_date: date
    landmark: DojiLandmark | None
    evidence: DetectionEvidence | None = None
    censor: CensorReason | None = None

    def __post_init__(self) -> None:
        if (self.evidence is None) == (self.censor is None):
            raise ValueError("detection requires evidence xor censor")


@dataclass(frozen=True, slots=True)
class DojiEvent:
    factor_id: DojiFactorId
    event_id: str
    symbol: str
    anchor_date: date
    bucket: SelectionBucket | None
    pit_status: PitUniverseStatus
    censor: CensorReason | None = None
    audit_code: DenominatorAuditCode | None = None

    def __post_init__(self) -> None:
        if self.pit_status is PitUniverseStatus.COVERAGE_MISSING:
            raise ValueError("missing universe coverage is order-level unavailable")


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, populate_by_name=True
    )


class DojiPatternsRequest(_Strict):
    symbols: list[str] = Field(min_length=1, max_length=200)
    start: date
    oos_start: date = DOJI_OOS_START_DEFAULT
    end: date
    theta_body_ratio: float = Field(default=DOJI_BODY_RATIO_MAX, gt=0, lt=1)
    cost_bps: float = Field(default=COST_BPS_DEFAULT, ge=0, le=COST_BPS_MAX)

    @field_validator("symbols")
    @classmethod
    def symbols_canonical(cls, value: list[str]) -> list[str]:
        import re

        normalized = [item.strip().upper() for item in value]
        if any(re.fullmatch(SYMBOL_PATTERN, item) is None for item in normalized) or len(
            set(normalized)
        ) != len(normalized):
            raise ValueError("symbols must be unique canonical A-share identifiers")
        return normalized

    @model_validator(mode="after")
    def dates_valid(self) -> DojiPatternsRequest:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class DojiCensor(_Strict):
    factor_id: DojiFactorId
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    event_date: date | None
    reason: CensorReason
    detail: str = ""


class DojiDenominatorAuditEntry(_Strict):
    event_id: str
    factor_id: DojiFactorId
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    event_date: date
    code: DenominatorAuditCode


class DojiExecutionSegment(_Strict):
    event_id: str
    factor_id: DojiFactorId
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    arm: DojiArm
    entry_date: date
    entry_quote_raw: float
    entry_cost_bps: float = Field(ge=0)
    holding_days: int = Field(ge=1)
    checkpoint_returns: dict[int, float] = Field(default_factory=dict)
    terminal_return: float
    liquidation_cost_adjusted_terminal_return: float
    mae: float
    mfe: float


class DojiFactorResult(_Strict):
    factor_id: DojiFactorId
    parent_events: int = Field(ge=0)
    qualified_events: int = Field(ge=0)
    not_selected_events: int = Field(ge=0)
    segments: list[DojiExecutionSegment] = Field(default_factory=list)
    censored: list[DojiCensor] = Field(default_factory=list)
    denominator_audit: list[DojiDenominatorAuditEntry] = Field(default_factory=list)
    is_diagnostic: dict[str, object] | None = Field(default=None, alias="is")
    oos: dict[str, object] = Field(default_factory=dict)
    diagnostics: dict[str, object] = Field(default_factory=dict)
    verdict: DojiVerdict


class DojiProvenance(_Strict):
    definition_version: Literal["v1"] = "v1"
    identities: DataIdentity
    required_columns: tuple[str, ...] = REQUIRED_CANONICAL_COLUMNS
    calendar_id: str
    parameters: dict[str, object]
    params_provenance: dict[str, str]
    code_version: str
    source_evidence_paths: tuple[str, ...] = SOURCE_EVIDENCE_PATHS


class DojiResponse(_Strict):
    status: DojiStatus
    definition_version: Literal["v1"] = "v1"
    factors: list[DojiFactorResult] = Field(default_factory=list)
    unavailable_reason: UnavailabilityReason | None = None
    provenance: DojiProvenance | None = None
    coverage: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def shape_valid(self) -> DojiResponse:
        if self.status is DojiStatus.OK and (
            len(self.factors) != len(DOJI_FACTOR_IDS)
            or tuple(item.factor_id for item in self.factors) != DOJI_FACTOR_IDS
            or self.unavailable_reason is not None
            or self.provenance is None
        ):
            raise ValueError("ok response requires ordered factors and provenance")
        if self.status is DojiStatus.UNAVAILABLE and (
            self.factors or self.unavailable_reason is None or self.coverage
        ):
            raise ValueError("unavailable response must be empty")
        return self


def validate_doji_factor_coverage(factors: Sequence[DojiFactorResult]) -> None:
    if (
        len(factors) != len(DOJI_FACTOR_IDS)
        or tuple(item.factor_id for item in factors) != DOJI_FACTOR_IDS
    ):
        raise ValueError("factors must be exactly the ordered doji factor IDs")


__all__ = [name for name in globals() if not name.startswith("_")]
