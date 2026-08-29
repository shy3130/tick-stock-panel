"""Frozen shared contract for Issue #46 retrieval-routing (MERA daily-proxy) research.

The response envelope follows the shared research contract
(schema/status/definition_version/request/identity/coverage/censored/events/
verdicts/promoted) and the repo style established by ``hold_firm_patterns``
and ``macd_stages``. All models are strict, frozen, and reject NaN/Inf so
that ``model_dump(mode="json")`` is directly serializable and auditable.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA = "tickflow.research.retrieval-routing.v1"
DEFINITION_VERSION = "v1"
DEFINITION_DOCUMENT = "docs/ISSUE-46/final-design.md"

# Frozen candidate grid: K and distance are selected ONLY on train+validation
# and then frozen before test is touched.
K_CANDIDATES: tuple[int, ...] = (5, 10, 20)
MAX_K = max(K_CANDIDATES)
DISTANCE_METRICS: tuple[str, ...] = ("euclidean", "cosine")

# Chronological split of unique panel dates.
SPLIT_RATIOS: tuple[float, float, float] = (0.6, 0.2, 0.2)

LABEL_CLASS_NAMES: tuple[str, str, str] = ("weak", "mid", "strong")
LABEL_QUANTILE_EDGES: tuple[float, float] = (1.0 / 3.0, 2.0 / 3.0)

# Hard gates; insufficient panels are explicitly unavailable, never degraded.
MIN_PANEL_SYMBOLS = 30
MIN_WARMED_SAMPLES_PER_EVAL_DATE = 20

DEFAULT_LABEL_HORIZON = 1
MAX_LABEL_HORIZON = 20
DEFAULT_COST_BPS = 10.0
MAX_COST_BPS = 1000.0
DEFAULT_PLACEBO_ROUNDS = 200
MIN_PLACEBO_ROUNDS = 20
MAX_PLACEBO_ROUNDS = 2000

# Fixed placebo seeds (documented in docs/ISSUE-46/final-design.md). Per-round
# generators derive as base_seed + round so any single round is reproducible.
PLACEBO_SEED_RANDOM_NEIGHBOR = 46051
PLACEBO_SEED_RANDOM_LABEL = 46052
PLACEBO_QUANTILE = 0.95

PLACEBO_KIND_RANDOM_NEIGHBOR = "random_neighbor"
PLACEBO_KIND_RANDOM_LABEL = "random_label"
PLACEBO_KINDS: tuple[str, str] = (PLACEBO_KIND_RANDOM_NEIGHBOR, PLACEBO_KIND_RANDOM_LABEL)

SYMBOL_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"

# Daily factor-zoo proxies used by the production panel seam. These are NOT
# the paper's minute-level MERA embeddings; see docs/ISSUE-46/feasibility.md.
DEFAULT_FEATURE_IDS: tuple[str, ...] = (
    "alpha101_004",
    "alpha101_006",
    "alpha101_009",
    "alpha101_012",
)

REQUIRED_BAR_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


class RoutingStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class RoutingVerdictStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class UnavailabilityReason(str, Enum):
    PANEL_COVERAGE = "unavailable_panel_coverage"
    NEIGHBOR_LEAKAGE = "unavailable_neighbor_leakage"
    LABEL_HORIZON_MISMATCH = "unavailable_label_horizon_mismatch"


class SplitName(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class ClaimId(str, Enum):
    RANK_IC_INCREMENT = "test_rank_ic_increment"
    COST_ADJUSTED_INCREMENT = "test_cost_adjusted_increment"


CLAIM_IDS: tuple[ClaimId, ClaimId] = (
    ClaimId.RANK_IC_INCREMENT,
    ClaimId.COST_ADJUSTED_INCREMENT,
)

CENSOR_LABEL_WINDOW = "censor_label_window_incomplete"
CENSOR_INSUFFICIENT_NEIGHBORS = "censor_insufficient_neighbors"


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class RetrievalRoutingRequest(_Strict):
    """Hyperparameters of one auditable retrieval-routing research run.

    The train/validation/test 60/20/20 date split, the candidate grid, the
    label quantiles, and the placebo seeds are definition-level constants and
    deliberately NOT request-tunable: a request can only loosen the audit,
    never the protocol.
    """

    label_horizon: int = Field(default=DEFAULT_LABEL_HORIZON, ge=1, le=MAX_LABEL_HORIZON)
    cost_bps: float = Field(default=DEFAULT_COST_BPS, ge=0.0, le=MAX_COST_BPS)
    placebo_rounds: int = Field(
        default=DEFAULT_PLACEBO_ROUNDS, ge=MIN_PLACEBO_ROUNDS, le=MAX_PLACEBO_ROUNDS
    )
    feature_names: list[str] | None = Field(
        default=None,
        description="Subset of panel feature names; None means all panel features.",
    )

    @model_validator(mode="after")
    def validate_features(self) -> "RetrievalRoutingRequest":
        if self.feature_names is not None:
            if not self.feature_names:
                raise ValueError("feature_names must be non-empty when provided")
            if len(set(self.feature_names)) != len(self.feature_names):
                raise ValueError("feature_names must be unique")
            if any(not isinstance(name, str) or not name.strip() for name in self.feature_names):
                raise ValueError("feature_names must contain non-empty strings")
        return self


class NeighborRecord(_Strict):
    neighbor_date: date
    label_available_date: date
    neighbor_symbol: str = Field(min_length=1)
    distance: float = Field(ge=0.0)
    label: int = Field(ge=0, le=2)


class RoutingEvent(_Strict):
    """One auditable routing decision for a validation/test query sample."""

    query_date: date
    symbol: str = Field(min_length=1)
    split: SplitName
    k_used: int = Field(ge=1)
    distance_metric: str
    neighbors: list[NeighborRecord] = Field(min_length=1)
    neighbor_label_mean: float = Field(ge=0.0, le=2.0)
    predicted_class: int = Field(ge=0, le=2)
    route_class: str
    routing_entropy: float = Field(ge=0.0, le=1.0)
    forward_return: float
    label: int = Field(ge=0, le=2)


class CensorRecord(_Strict):
    code: str = Field(min_length=1)
    detail: str = ""
    count: int = Field(ge=0)
    first_date: date | None = None
    last_date: date | None = None


class CoverageReport(_Strict):
    symbols: int = Field(ge=0)
    dates: int = Field(ge=0)
    warmed_samples: int = Field(ge=0)
    eligible_samples: int = Field(ge=0)
    train_dates: int = Field(ge=0)
    validation_dates: int = Field(ge=0)
    test_dates: int = Field(ge=0)
    train_samples: int = Field(ge=0)
    validation_samples: int = Field(ge=0)
    test_samples: int = Field(ge=0)
    min_eligible_per_eval_date: int = Field(ge=0)
    eval_dates_below_gate: list[date] = Field(default_factory=list)
    degenerate_features: list[str] = Field(default_factory=list)
    train_label_counts: dict[str, int] = Field(default_factory=dict)


class FrozenStatistics(_Strict):
    """Train-window statistics frozen before validation/test are touched."""

    feature_names: list[str] = Field(min_length=1)
    feature_means: dict[str, float]
    feature_stds: dict[str, float]
    label_quantile_edges: list[float]
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date


class SplitMetrics(_Strict):
    split: SplitName
    dates: int = Field(ge=0)
    samples: int = Field(ge=0)
    queries: int = Field(ge=0)
    censored_queries: int = Field(ge=0)
    rank_ic_dates: int = Field(ge=0)
    routing_rank_ic: float
    baseline_feature: str = Field(min_length=1)
    baseline_rank_ic: float
    rank_ic_increment: float
    long_short_gross: float | None = None
    long_short_cost: float | None = None
    long_short_net: float | None = None
    cost_adjusted_increment: float | None = None


class PlaceboResult(_Strict):
    kind: str
    claim: ClaimId
    rounds: int = Field(ge=1)
    real_increment: float
    placebo_mean: float
    placebo_q95: float
    blocked: bool


class ClaimVerdict(_Strict):
    claim: ClaimId
    verdict: RoutingVerdictStatus
    evidence: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


class Provenance(_Strict):
    definition_version: Literal["v1"] = "v1"
    definition_document: str = DEFINITION_DOCUMENT
    panel: dict[str, Any] = Field(default_factory=dict)
    frozen: FrozenStatistics
    selection: dict[str, Any] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()


class RetrievalRoutingResponse(_Strict):
    schema_: Literal["tickflow.research.retrieval-routing.v1"] = Field(
        default=SCHEMA,
        alias="schema",
    )
    status: RoutingStatus
    definition_version: Literal["v1"] = "v1"
    request: RetrievalRoutingRequest
    identity: dict[str, Any] = Field(default_factory=dict)
    coverage: CoverageReport | None = None
    censored: list[CensorRecord] = Field(default_factory=list)
    events: list[RoutingEvent] = Field(default_factory=list)
    verdicts: list[ClaimVerdict] = Field(default_factory=list)
    promoted: Literal[False] = False
    unavailable_reason: UnavailabilityReason | None = None
    unavailable_detail: str = ""
    splits: list[SplitMetrics] = Field(default_factory=list)
    placebos: list[PlaceboResult] = Field(default_factory=list)
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def validate_envelope(self) -> "RetrievalRoutingResponse":
        if self.promoted is not False:
            raise ValueError("research responses are never promoted")
        if self.status is RoutingStatus.OK:
            if self.unavailable_reason is not None:
                raise ValueError("ok response must not carry an unavailable_reason")
            if self.coverage is None or self.provenance is None or not self.splits:
                raise ValueError("ok response requires coverage, provenance, and splits")
            if tuple(v.claim for v in self.verdicts) != CLAIM_IDS:
                raise ValueError("ok response requires exactly the two claim verdicts")
        else:
            if self.unavailable_reason is None:
                raise ValueError("unavailable response requires a reason")
            if self.events or self.verdicts or self.splits:
                raise ValueError("unavailable response must not carry partial results")
        return self


def unavailable_response(
    request: RetrievalRoutingRequest,
    reason: UnavailabilityReason,
    detail: str = "",
    identity: dict[str, Any] | None = None,
    coverage: CoverageReport | None = None,
    censored: list[CensorRecord] | None = None,
) -> RetrievalRoutingResponse:
    """Fail-closed envelope: research insufficiency is explicit, never degraded."""
    return RetrievalRoutingResponse(
        status=RoutingStatus.UNAVAILABLE,
        request=request,
        identity=identity or {},
        coverage=coverage,
        censored=censored or [],
        unavailable_reason=reason,
        unavailable_detail=detail,
    )


__all__ = [
    "SCHEMA",
    "DEFINITION_VERSION",
    "DEFINITION_DOCUMENT",
    "K_CANDIDATES",
    "MAX_K",
    "DISTANCE_METRICS",
    "SPLIT_RATIOS",
    "LABEL_CLASS_NAMES",
    "LABEL_QUANTILE_EDGES",
    "MIN_PANEL_SYMBOLS",
    "MIN_WARMED_SAMPLES_PER_EVAL_DATE",
    "DEFAULT_LABEL_HORIZON",
    "MAX_LABEL_HORIZON",
    "DEFAULT_COST_BPS",
    "MAX_COST_BPS",
    "DEFAULT_PLACEBO_ROUNDS",
    "MIN_PLACEBO_ROUNDS",
    "MAX_PLACEBO_ROUNDS",
    "PLACEBO_SEED_RANDOM_NEIGHBOR",
    "PLACEBO_SEED_RANDOM_LABEL",
    "PLACEBO_QUANTILE",
    "PLACEBO_KINDS",
    "PLACEBO_KIND_RANDOM_NEIGHBOR",
    "PLACEBO_KIND_RANDOM_LABEL",
    "SYMBOL_PATTERN",
    "DEFAULT_FEATURE_IDS",
    "REQUIRED_BAR_COLUMNS",
    "CLAIM_IDS",
    "CENSOR_LABEL_WINDOW",
    "CENSOR_INSUFFICIENT_NEIGHBORS",
    "RoutingStatus",
    "RoutingVerdictStatus",
    "UnavailabilityReason",
    "SplitName",
    "ClaimId",
    "RetrievalRoutingRequest",
    "NeighborRecord",
    "RoutingEvent",
    "CensorRecord",
    "CoverageReport",
    "FrozenStatistics",
    "SplitMetrics",
    "PlaceboResult",
    "ClaimVerdict",
    "Provenance",
    "RetrievalRoutingResponse",
    "unavailable_response",
]
