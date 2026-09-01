"""Research Workbench V2 wire contracts.

Parameter models mirror existing evaluator request schemas. Scope owns symbols
so factor parameters never duplicate them.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESEARCH_CONTRACT_SCHEMA = "tickflow.research.contracts.v2"

FactorId = str

RunScopeType = Literal["symbols", "full_market"]
JobStatus = Literal["pending", "running", "interrupted", "completed", "failed", "cancelled"]
ResearchVerdict = Literal["accepted", "rejected", "unavailable", "inconclusive"]
DataStatus = Literal["ready", "partial", "missing", "stale", "censored"]
PromotionStatus = Literal["not_promoted", "candidate", "promoted"]
ResultProfile = Literal[
    "arm_comparison",
    "event_signal",
    "shape_distribution",
    "retrieval",
    "calendar_effect",
]
EngineeringStatus = Literal["completed", "partial", "planned"]
FactorCategory = Literal[
    "pattern", "event", "intraday", "trend", "retrieval", "calendar", "exclusion"
]
DataRequirementKind = Literal[
    "canonical", "markets", "minutes", "trans", "index_daily", "universe", "calendar"
]
SSEEventType = Literal[
    "snapshot",
    "progress",
    "warning",
    "interrupted",
    "completed",
    "failed",
    "cancelled",
    "heartbeat",
]
SeriesKind = Literal["equity", "baseline", "increment", "drawdown"]

_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


def normalize_symbols(values: list[str]) -> list[str]:
    """Canonical A-share symbol normalization shared with legacy routes."""
    normalized = [str(value).strip().upper() for value in values]
    if any(_SYMBOL_RE.fullmatch(value) is None for value in normalized):
        raise ValueError("symbols must be canonical A-share identifiers")
    if len(normalized) != len(set(normalized)):
        raise ValueError("symbols must be unique")
    return normalized


class RunScopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RunScopeType
    symbols: list[str] | None = None

    @field_validator("symbols")
    @classmethod
    def canonical_symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return normalize_symbols(value)

    @model_validator(mode="after")
    def scope_shape(self) -> RunScopeModel:
        if self.type == "symbols" and not self.symbols:
            raise ValueError("symbols scope requires at least one symbol")
        if self.type == "full_market" and self.symbols is not None:
            raise ValueError("full_market scope must not contain symbols")
        return self


# ---------------------------------------------------------------------------
# Per-factor parameter models (19). Scope owns symbols; parameters never do.
# ---------------------------------------------------------------------------


class _DatedWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date

    @model_validator(mode="after")
    def valid_window(self) -> _DatedWindow:
        if self.start > self.end:
            raise ValueError("start must be <= end")
        return self


class _OosSplit(_DatedWindow):
    oos_start: date

    @model_validator(mode="after")
    def valid_split(self) -> _OosSplit:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class NShapeParameters(_DatedWindow):
    """app.services.n_shape_golden_phoenix.evaluate_n_shape"""


class MTFDirectionParameters(_OosSplit):
    """app.services.mtf_direction_15m5m.evaluate_mtf_direction"""

    @model_validator(mode="after")
    def bounded_window(self) -> MTFDirectionParameters:
        if (self.end - self.start) > timedelta(days=370):
            raise ValueError("window must be <= 370 days")
        return self


class WeakToStrongParameters(BaseModel):
    """app.services.weak_to_strong.WeakToStrongEvaluateRequest minus symbols."""

    model_config = ConfigDict(extra="forbid")

    signal_date: date
    oos_start: date | None = None
    cost_bps: float = Field(default=20.0, ge=0, le=500)

    @field_validator("signal_date")
    @classmethod
    def reject_future_date(cls, value: date) -> date:
        if value > date.today():
            raise ValueError(f"signal_date {value.isoformat()} is in the future")
        return value


class VolumeBreakoutParameters(_OosSplit):
    """app.services.volume_breakout.evaluate_volume_breakout"""

    cost_bps: float = Field(default=10.0, ge=0)


class MacdArmsParameters(_OosSplit):
    """Frozen evaluator arms with a fixed 20 bps round-trip cost."""


class SingleYangParameters(_OosSplit):
    """app.services.single_yang_no_break.evaluate_single_yang (+increment)"""

    cost_bps: float = Field(default=10.0, ge=0)


class ZuoyiDefenseParameters(_OosSplit):
    """app.services.zuoyi_defense.evaluate_zuoyi_defense"""

    cost_bps: float = Field(default=10.0, ge=0, le=1000)


class DailyOpenAnchorParameters(_OosSplit):
    """app.services.daily_open_anchor_filter.evaluate_daily_open_anchor"""


class HoldFirmParameters(_OosSplit):
    """app.services.hold_firm_patterns.evaluate_hold_firm_patterns"""

    cost_bps: float = Field(default=10.0, ge=0, le=1000)


class DuguTrendParameters(BaseModel):
    """app.services.daily_event_research.DailyEventRequest minus symbols."""

    model_config = ConfigDict(extra="forbid")

    variant: Literal["ma_24_72", "ma_20_70"]
    band_mode: Literal["fixed", "atr"] = "fixed"
    require_m3: bool = False
    alignment_days: Literal[10, 30, 50, 100] = 30
    start: date
    oos_start: date
    end: date
    horizon_days: int = Field(default=20, ge=1, le=60)
    cost_bps: float = Field(default=10.0, ge=0, le=1000)

    @model_validator(mode="after")
    def valid_split(self) -> DuguTrendParameters:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class MeraParameters(_DatedWindow):
    """app.services.retrieval_routing_research panel + routing request."""

    label_horizon: int = Field(default=1, ge=1, le=20)
    cost_bps: float = Field(default=10.0, ge=0, le=1000)
    placebo_rounds: int = Field(default=200, ge=50, le=1000)
    feature_names: list[str] | None = None


class PreSurgeParameters(BaseModel):
    """app.services.daily_event_research.production.evaluate_pre_surge_production"""

    model_config = ConfigDict(extra="forbid")

    start: date
    oos_start: date
    end: date
    benchmark_symbol: str = Field(default="000300.SH", pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    cost_bps: float = Field(default=10.0, ge=0, le=1000)

    @model_validator(mode="after")
    def valid_split(self) -> PreSurgeParameters:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class EscapeRiskParameters(BaseModel):
    """app.services.daily_event_research.production.evaluate_escape_risk_production"""

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    oos_start: date
    cost_bps: float = Field(default=10.0, ge=0, le=1000)

    @model_validator(mode="after")
    def valid_split(self) -> EscapeRiskParameters:
        if self.start > self.end:
            raise ValueError("start must be <= end")
        if not self.start <= self.oos_start <= self.end:
            raise ValueError("oos_start must be within [start, end]")
        return self


class NDepthParameters(_DatedWindow):
    """app.services.n_shape_pullback_depth.evaluate_n_shape_pullback_depth"""

    reversal_mode: Literal["fixed_pct", "atr_multiple"] = "fixed_pct"
    reversal_value: float = Field(default=0.08, gt=0, le=10.0)
    cost_bps: float = Field(default=20.0, ge=0, le=1000)

    @model_validator(mode="after")
    def valid_reversal(self) -> NDepthParameters:
        if self.reversal_mode == "fixed_pct" and self.reversal_value >= 1:
            raise ValueError("fixed_pct reversal_value must be < 1")
        return self


class NegativeExclusionParameters(BaseModel):
    """app.services.negative_exclusion_production.evaluate_negative_exclusion_production"""

    model_config = ConfigDict(extra="forbid")

    start: date
    oos_start: date
    end: date
    enabled_classes: list[Literal["v2", "v4", "v5"]] | None = None
    horizon_days: int = Field(default=10, ge=1, le=60)
    cost_bps: float = Field(default=20.0, ge=0, le=1000)

    @field_validator("enabled_classes")
    @classmethod
    def unique_classes(
        cls, value: list[Literal["v2", "v4", "v5"]] | None
    ) -> list[Literal["v2", "v4", "v5"]] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("enabled_classes must be unique")
        return value

    @model_validator(mode="after")
    def valid_split(self) -> NegativeExclusionParameters:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class DojiParameters(BaseModel):
    """app.services.doji_patterns.evaluate_doji_patterns"""

    model_config = ConfigDict(extra="forbid")

    start: date
    oos_start: date = date(2025, 7, 1)
    end: date
    theta_body_ratio: float = Field(default=0.10, gt=0, lt=1)
    cost_bps: float = Field(default=10.0, ge=0, le=1000)

    @model_validator(mode="after")
    def valid_split(self) -> DojiParameters:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class ChipPeakParameters(BaseModel):
    """app.services.chip_peak_patterns.evaluate"""

    model_config = ConfigDict(extra="forbid")

    start: date
    oos_start: date = date(2025, 7, 1)
    end: date
    cost_bps: float = Field(default=10.0, ge=0, le=1000)

    @model_validator(mode="after")
    def valid_split(self) -> ChipPeakParameters:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class WeeklyFlagpoleParameters(_OosSplit):
    """app.services.weekly_flagpole.evaluate"""

    cost_bps: float = Field(default=10.0, ge=0)


class EscapeWindowsParameters(_DatedWindow):
    """app.services.escape_windows.evaluate_escape_windows"""

    start: date = date(2007, 1, 1)
    end: date = date(2026, 12, 31)


PARAMETER_MODELS: dict[str, type[BaseModel]] = {
    "n-shape": NShapeParameters,
    "mtf-direction": MTFDirectionParameters,
    "weak-to-strong": WeakToStrongParameters,
    "volume-breakout": VolumeBreakoutParameters,
    "macd-arms": MacdArmsParameters,
    "single-yang-no-break": SingleYangParameters,
    "zuoyi-defense": ZuoyiDefenseParameters,
    "daily-open-anchor": DailyOpenAnchorParameters,
    "hold-firm": HoldFirmParameters,
    "dugu-trend": DuguTrendParameters,
    "mera": MeraParameters,
    "pre-surge": PreSurgeParameters,
    "escape-risk": EscapeRiskParameters,
    "n-depth": NDepthParameters,
    "negative-exclusion": NegativeExclusionParameters,
    "doji-patterns": DojiParameters,
    "chip-peak-patterns": ChipPeakParameters,
    "weekly-flagpole": WeeklyFlagpoleParameters,
    "escape-windows": EscapeWindowsParameters,
}


# ---------------------------------------------------------------------------
# Catalog / preflight / run / result / error envelopes.
# ---------------------------------------------------------------------------


class DataRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: DataRequirementKind
    description: str = ""


class ParameterField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["symbol_list", "date", "number", "integer", "boolean", "enum", "multi_enum"]
    label: str | None = None
    required: bool = False
    default: Any = None
    options: list[Any] = Field(default_factory=list)
    description: str | None = None


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    category: FactorCategory
    description: str
    engineering_status: EngineeringStatus
    latest_data_status: DataStatus | None = None
    latest_verdict: ResearchVerdict | None = None
    promotion_status: PromotionStatus = "not_promoted"
    supported_scopes: list[RunScopeType]
    result_profile: ResultProfile
    data_requirements: list[DataRequirementKind]
    todo_status: Literal["completed", "in_progress"]
    docs: list[str] = Field(default_factory=list)
    known_gaps: list[str] = Field(default_factory=list)


class FactorDetail(CatalogEntry):
    parameter_schema: dict[str, Any]
    parameter_fields: list[ParameterField] = Field(default_factory=list)
    arms: list[dict[str, Any]] = Field(default_factory=list)
    strongest_baseline: str | None = None
    acceptance_gates: list[str] = Field(default_factory=list)
    provenance_requirements: list[str] = Field(default_factory=list)
    latest_runs: list[dict[str, Any]] = Field(default_factory=list)


class PreflightSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    status: str
    generation: str | None = None
    manifest_sha256: str | None = None
    available_from: date | None = None
    available_to: date | None = None


class BlockingReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    source: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CohortEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_symbols: int = 0
    eligible_symbols: int = 0
    censored_symbols: int = 0


class ResourceEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_class: Literal["interactive", "full_market"] = "interactive"
    full_market_supported: bool = False


class PreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_id: str
    scope: RunScopeModel
    parameters: dict[str, Any] = Field(default_factory=dict)


class PreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    factor_id: str
    normalized_request: dict[str, Any] = Field(default_factory=dict)
    sources: list[PreflightSource] = Field(default_factory=list)
    cohort: CohortEstimate = Field(default_factory=CohortEstimate)
    warnings: list[str] = Field(default_factory=list)
    blocking_reasons: list[BlockingReason] = Field(default_factory=list)
    resource_estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)


class RunCreateRequest(PreflightRequest):
    source_run_id: str | None = None



class RunEvidenceLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    summary: str = ""

class RunPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=200)
    favorite: bool | None = None


class RunLinks(BaseModel):
    self: str
    stream: str
    events: str


class RunAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    job_status: JobStatus
    factor_id: str
    scope: RunScopeModel
    created_at: str
    links: RunLinks


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    factor_id: str
    profile: ResultProfile
    scope: RunScopeModel
    parameters: dict[str, Any]
    job_status: JobStatus
    verdict: ResearchVerdict = "inconclusive"
    data_status: DataStatus = "missing"
    promotion_status: PromotionStatus = "not_promoted"
    label: str | None = None
    favorite: bool = False
    source_run_id: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    preflight: dict[str, Any] = Field(default_factory=dict)
    summary_available: bool = False
    summary: dict[str, Any] | None = None
    artifact: dict[str, Any] | None = None
    links: RunLinks | None = None


class EventTableRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str | None = None
    arm: str | None = None
    event_date: date | None = None
    qualified: bool | None = None
    reachable: bool | None = None
    censor_code: str | None = None
    label: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class NormalizedResearchResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    profile: ResultProfile
    status: Literal["ready", "unavailable"]
    verdict: ResearchVerdict
    summary: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    events: list[EventTableRow] = Field(default_factory=list)
    series: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    horizons: list[int] = Field(default_factory=list)
    risk: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    unavailable_reasons: list[dict[str, Any]] = Field(default_factory=list)


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    field: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    event_type: SSEEventType
    run_id: str
    ts: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CancellationToken:
    """Cooperative cancellation checked between evaluator stages."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeError("research_cancelled")


class ProgressReporter:
    """Stage progress funnel; runner binds it to the job store event log."""

    def __init__(self, callback: Any = None) -> None:
        self._callback = callback

    def report(self, stage: str, percent: float, message: str = "") -> None:
        if self._callback is not None:
            self._callback(stage, max(0.0, min(100.0, percent)), message)
