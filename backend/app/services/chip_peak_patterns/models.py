"""Frozen contracts for local turnover-decay chip peak research."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFINITION_VERSION = "chip-peak-v1"
DEFINITION_DOCUMENT = "backend/app/services/chip_peak_patterns/__init__.py"
FACTOR_IDS = (
    "c1_low_single_peak",
    "c2_double_peak_hold",
    "c3_peak_relocation",
    "c4_dense_breakout",
    "c5_dispersed_exclusion",
)
FactorId = Literal[
    "c1_low_single_peak",
    "c2_double_peak_hold",
    "c3_peak_relocation",
    "c4_dense_breakout",
    "c5_dispersed_exclusion",
]
OOS_START_DEFAULT = date(2025, 7, 1)
COST_BPS_DEFAULT = 10.0
COST_BPS_MAX = 1000.0
MIN_OOS_EVENTS, MIN_OOS_SYMBOLS = 30, 10
BOOTSTRAP_SEED, BOOTSTRAP_ROUNDS, MIN_VALID_BOOTSTRAP_REPLICATES = 42, 5000, 4750
CI_LEVEL, CI_LOWER_QUANTILE, CI_UPPER_QUANTILE = 0.95, 0.025, 0.975
WARMUP_MARKET_DAYS, FORWARD_BUDGET_MARKET_DAYS = 250, 80
FORWARD_HORIZONS = (20, 60)
EX_DIV_COOLDOWN_MARKET_DAYS = 10
LOOKBACK_CALENDAR_DAYS, FORWARD_CALENDAR_DAYS = 500, 180
PRICE_ABS_TOL, MAX_GRID_CELLS = 0.005, 200_000
SYMBOL_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"
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
BETA_ARMS = ("turnover", "turnover_x0.5", "turnover_x2", "geometric_0.01")
MAIN_ARM, GEOMETRIC_BETA = "turnover", 0.01
C1_MAX_PEAKS, C1_MAX_PRICE_PCT, C1_MIN_WINNER, C1_MIN_CONCENTRATION = 1, 0.35, 0.85, 0.70
C2_MIN_PEAKS, C2_MIN_LOW_PEAK_SHARE, C2_LOOKBACK, C2_HOLD_TOL, C2_HOLLOW_DROP, C2_UPTREND_PCT = (
    2,
    0.40,
    20,
    0.10,
    0.30,
    0.50,
)
C3_WINDOW, C3_LOW_MAIN_PCT, C3_HIGH_MAIN_PCT, C3_PRIOR_RUN_SPLIT = 20, 0.35, 0.65, 0.30
C4_MIN_CONCENTRATION, C4_MIN_WINNER = 0.60, 0.60
C5_MIN_PEAKS, C5_MAX_CONCENTRATION = 3, 0.40


class BetaArm(StrEnum):
    TURNOVER = "turnover"
    TURNOVER_HALF = "turnover_x0.5"
    TURNOVER_DOUBLE = "turnover_x2"
    GEOMETRIC = "geometric_0.01"


class ChipStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class ChipVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class ChipCensorReason(StrEnum):
    MISSING_PIT_TURNOVER = "censor_missing_pit_turnover"
    MISSING_AVAILABLE_AT = "censor_turnover_available_at_missing"
    WARMUP_INCOMPLETE = "censor_warmup_incomplete"
    HORIZON_INCOMPLETE = "censor_horizon_incomplete"
    ENTRY_BAR_MISSING = "censor_entry_bar_missing"
    ENTRY_OPEN_INVALID = "censor_entry_open_invalid"
    ENTRY_UNREACHABLE = "censor_entry_unreachable"
    EX_DIV_COOLDOWN = "censor_ex_div_cooldown"
    EVENT_OVERLAP = "censor_same_factor_symbol_overlap"
    PIT_UNIVERSE_INELIGIBLE = "censor_pit_universe_ineligible"
    UNIVERSE_COVERAGE_MISSING = "censor_universe_coverage_missing"


class UnavailabilityReason(StrEnum):
    CANONICAL_READER = "unavailable_canonical_reader"
    MARKET_FACTS_INCOMPLETE = "unavailable_market_facts_incomplete"
    UNIVERSE_PRESENCE = "unavailable_universe_presence"
    TURNOVER_PROVENANCE = "unavailable_pit_turnover_provenance"
    BETA_INSTABILITY = "unavailable_beta_arm_instability"
    BOOTSTRAP = "unavailable_bootstrap_min_valid_replicates"
    INVALID_PROVENANCE = "unavailable_invalid_provenance"


class ChipModelParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    price_step_pct: float = Field(0.01, gt=0, le=0.05)
    kernel_sigma_pct: float = Field(0.02, gt=0, le=0.10)
    peak_min_prominence: float = Field(0.05, gt=0, lt=1)
    low_chip_price_ratio: float = Field(0.80, gt=0, lt=1)
    concentration_band_pct: float = Field(0.10, gt=0, le=0.25)
    warmup_market_days: int = Field(WARMUP_MARKET_DAYS, ge=1)
    ex_div_cooldown_market_days: int = Field(EX_DIV_COOLDOWN_MARKET_DAYS, ge=0)
    max_grid_cells: int = Field(MAX_GRID_CELLS, ge=1000)


class ChipBar:
    __slots__ = (
        "amount",
        "close",
        "date",
        "high",
        "low",
        "open",
        "raw_close",
        "raw_high",
        "raw_low",
        "raw_open",
        "symbol",
        "volume",
    )

    def __init__(
        self,
        *,
        symbol: str,
        date: date,
        open: float,
        high: float,
        low: float,
        close: float,
        raw_open: float,
        raw_high: float,
        raw_low: float,
        raw_close: float,
        volume: float,
        amount: float,
    ) -> None:
        self.symbol, self.date, self.open, self.high, self.low, self.close = (
            symbol,
            date,
            open,
            high,
            low,
            close,
        )
        self.raw_open, self.raw_high, self.raw_low, self.raw_close = (
            raw_open,
            raw_high,
            raw_low,
            raw_close,
        )
        self.volume, self.amount = volume, amount

    @property
    def adj_ratio(self) -> float:
        return self.close / self.raw_close if self.raw_close > 0 else 1.0


class TurnoverDay:
    __slots__ = (
        "availability_basis",
        "available_at",
        "float_shares",
        "reported_turnover_pct",
        "source_day",
    )

    def __init__(
        self,
        *,
        available_at: date | None,
        reported_turnover_pct: float | None = None,
        float_shares: float | None = None,
        source_day: date | None = None,
        availability_basis: str | None = None,
    ) -> None:
        self.available_at = available_at
        self.reported_turnover_pct = reported_turnover_pct
        self.float_shares = float_shares
        self.source_day = source_day
        self.availability_basis = availability_basis


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, populate_by_name=True
    )


class ChipTurnoverIdentity(_Strict):
    source: Literal["published_daily_markets_hslv_or_lagged_ltgb"]
    rows: int = Field(ge=0)
    symbols: int = Field(ge=0)


class ChipDataIdentity(_Strict):
    canonical: object
    markets: object
    universe: object
    turnover: ChipTurnoverIdentity | None = None


class ChipPeakRequest(_Strict):
    symbols: list[str] = Field(min_length=1, max_length=200)
    start: date
    oos_start: date = OOS_START_DEFAULT
    end: date
    cost_bps: float = Field(COST_BPS_DEFAULT, ge=0, le=COST_BPS_MAX)

    @field_validator("symbols")
    @classmethod
    def symbols_valid(cls, values: list[str]) -> list[str]:
        import re

        values = [v.strip().upper() for v in values]
        if any(re.fullmatch(SYMBOL_PATTERN, v) is None for v in values) or len(values) != len(
            set(values)
        ):
            raise ValueError("symbols must be unique canonical A-share identifiers")
        return values

    @model_validator(mode="after")
    def dates_valid(self) -> ChipPeakRequest:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class ChipCensor(_Strict):
    factor_id: FactorId
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    event_date: date | None
    reason: ChipCensorReason
    detail: str = ""


class ChipDenominatorAuditEntry(_Strict):
    event_id: str
    factor_id: FactorId
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    event_date: date
    code: str


class ChipBetaStability(_Strict):
    main_arm: BetaArm = BetaArm.TURNOVER
    arm_directions: dict[str, Literal["positive", "negative", "flat"] | None] = {}
    consensus: Literal["positive", "negative", "flat"] | None = None
    consensus_votes: int = Field(0, ge=0)
    stable: bool = False
    detail: str = ""


class ChipFactorResult(_Strict):
    factor_id: FactorId
    parent_events: int = Field(ge=0)
    qualified_events: int = Field(ge=0)
    control_events: int = Field(ge=0)
    qualified_bucket: str
    control_bucket: str
    censored: list[ChipCensor] = []
    denominator_audit: list[ChipDenominatorAuditEntry] = []
    is_diagnostic: dict[str, object] | None = None
    oos: dict[str, object] = {}
    diagnostics: dict[str, object] = {}
    beta_stability: ChipBetaStability
    phase2_pending: bool = False
    phase2_note: str | None = None
    verdict: ChipVerdict


class ChipSymbolAudit(_Strict):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    status: Literal["ok", "no_bars", "insufficient_history", "missing_pit_turnover"]
    detail: str = ""


class ChipProvenance(_Strict):
    definition_version: Literal["chip-peak-v1"] = DEFINITION_VERSION
    identities: ChipDataIdentity
    required_columns: tuple[str, ...] = REQUIRED_CANONICAL_COLUMNS
    calendar_id: str
    parameters: dict[str, object]
    params_provenance: dict[str, str]
    code_version: str = DEFINITION_VERSION
    source_evidence_paths: tuple[str, ...] = (DEFINITION_DOCUMENT,)


class ChipPeakResponse(_Strict):
    status: ChipStatus
    definition_version: Literal["chip-peak-v1"] = DEFINITION_VERSION
    factors: list[ChipFactorResult] = []
    symbol_audit: list[ChipSymbolAudit] = []
    arms_evaluated: tuple[str, ...] = ()
    unavailable_reason: UnavailabilityReason | None = None
    provenance: ChipProvenance | None = None

    @model_validator(mode="after")
    def response_valid(self) -> ChipPeakResponse:
        if self.status is ChipStatus.OK and (
            len(self.factors) != 5 or tuple(x.factor_id for x in self.factors) != FACTOR_IDS
        ):
            raise ValueError("ok response requires five ordered factors")
        if self.status is ChipStatus.UNAVAILABLE and (
            self.factors or self.unavailable_reason is None
        ):
            raise ValueError("unavailable response must be empty")
        return self


class CapabilityResult(_Strict):
    status: ChipStatus
    identities: ChipDataIdentity | None = None
    problems: tuple[str, ...] = ()


class ArmResearch(_Strict):
    arm: BetaArm
    qualified_events: int = Field(ge=0)
    qualified_symbols: int = Field(ge=0)
    control_events: int = Field(ge=0)
    control_symbols: int = Field(ge=0)
    oos_gate_passed: bool
    bootstrap: dict[str, object] = {}
    direction: Literal["positive", "negative", "flat"] | None = None
    verdict: ChipVerdict = ChipVerdict.UNAVAILABLE
