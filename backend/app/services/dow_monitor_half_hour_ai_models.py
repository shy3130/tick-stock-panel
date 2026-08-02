from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HalfHourAiStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "insufficient_data",
    "unavailable",
]
HourlyAiRerunStatus = Literal["queued", "running", "completed", "failed"]
TrendBias = Literal["BULLISH", "BEARISH", "NEUTRAL", "TRANSITION"]
OpportunityChange = Literal[
    "STRENGTHENING",
    "WEAKENING",
    "UNCHANGED",
    "REVERSING",
]
AdviceState = Literal[
    "FOCUS",
    "WAIT_CONFIRMATION",
    "HOLD_OBSERVE",
    "DEFENSIVE",
    "AVOID_CHASING",
    "REDUCE_RISK",
]


class StageHeadline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    trend_bias: TrendBias
    opportunity_change: OpportunityChange
    summary: str


class StagePathSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: str
    description: str
    metric_keys: tuple[str, ...] = ()


class ChannelAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Literal["UP", "DOWN", "RANGE", "TRANSITION"]
    maturity: Literal["FORMING", "CONFIRMED", "FAILED", "NONE"]
    explanation: str
    evidence_metric_keys: tuple[str, ...] = ()


class PatternAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["FORMING", "CONFIRMED", "FAILED", "NONE"]
    explanation: str
    evidence_metric_keys: tuple[str, ...] = ()
    invalidation_metric_keys: tuple[str, ...] = ()


class PositionAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: AdviceState
    advice: str
    conditions: tuple[str, ...] = ()


class NextStageConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strengthen: tuple[str, ...] = ()
    risk: tuple[str, ...] = ()
    invalidation: tuple[str, ...] = ()


class HourlyStageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: StageHeadline
    stage_path: list[StagePathSegment]
    hidden_changes: list[str]
    comparison_with_previous: str
    day_overview: str
    channel: ChannelAssessment
    patterns: list[PatternAssessment]
    volume_capital_interpretation: str
    holding_advice: PositionAdvice
    watching_advice: PositionAdvice
    next_stage_conditions: NextStageConditions
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


class ValidatedEvidence(BaseModel):
    metric_key: str
    label: str
    value: str
    meaning: str


class AnalysisScenario(BaseModel):
    condition: str
    implication: str
    invalidates_when: str


class HalfHourAiSummary(BaseModel):
    analysis_id: str | None = None
    market: Literal["cn", "hk", "us"]
    symbol: str
    trade_date: date
    window_end: datetime | None = None
    status: HalfHourAiStatus
    report_frequency: Literal["half_hour", "hourly"] = "half_hour"
    stage_start: datetime | None = None
    stage_trading_minutes: int | None = Field(default=None, ge=0, le=1440)
    opportunity_change: OpportunityChange | None = None
    title: str | None = None
    summary: str | None = None
    updated_at: datetime


class HalfHourAiAnalysis(HalfHourAiSummary):
    data_cutoff: datetime
    model_name: str | None = None
    conclusion: str | None = None
    evidence: list[ValidatedEvidence] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    scenarios: list[AnalysisScenario] = Field(default_factory=list)
    data_quality: list[str] = Field(default_factory=list)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    report: HourlyStageReport | None = None
    attempt: int = Field(default=1, ge=1, le=65535)
    error_code: str | None = None
    error_message: str | None = None


class HourlyAiRerunRequest(BaseModel):
    request_id: str
    analysis_id: str
    market: Literal["cn", "hk", "us"]
    symbol: str
    trade_date: date
    window_end: datetime
    data_cutoff: datetime
    status: HourlyAiRerunStatus
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime
    error_code: str | None = None
    error_message: str | None = None


def analysis_id_for(
    market: str,
    symbol: str,
    trade_date: date,
    window_end: datetime,
) -> str:
    if window_end.tzinfo is None or window_end.utcoffset() is None:
        raise ValueError("window_end must be timezone-aware")
    logical_key = "|".join(
        (
            market.lower(),
            symbol.strip().upper(),
            trade_date.isoformat(),
            window_end.astimezone(UTC).isoformat(),
        )
    )
    return hashlib.sha256(logical_key.encode("utf-8")).hexdigest()
