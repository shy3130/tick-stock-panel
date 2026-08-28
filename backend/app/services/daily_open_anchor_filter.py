"""Auditable daily-open-anchor entry filter research (Issue #30 final-design).

Sealed-only. Inputs are one pinned canonical generation (adjusted + raw OHLC,
``PublishedCanonicalDailyReader``) and the immutable markets facts pin taken
from ``canonical_manifest["source_generations"]["markets"]`` via the shared
``load_pinned_market_facts`` contract.

Contract highlights (docs/ISSUE-30/final-design.md):
- raw-scale PIT limit bands: ``upper/lower = ROUND_HALF_UP(pre_close*(1±ratio), 0.01)``
  rebuilt from the raw previous close + regime rule and cross-checked against the
  published limit-up price; any missing fact fails the whole request closed.
- ``signal_limit_up/down`` booleans are derived on the raw scale
  (``raw_high >= upper`` / ``raw_low <= lower``) and then attached to the
  forward-adjusted panel consumed by the matcher.
- exact T+1 comes from the pinned market calendar; every candidate is matched by
  exactly one ``simulate_independent_candidates`` call on a continuous
  single-symbol panel (T..T+1+15), so the engine's aggregate block counters map
  one-to-one onto a per-candidate terminal ledger.
- ``sell_no_future`` is unreachable under the verified continuous horizon and is
  therefore not a terminal outcome (review-v3 P2).
- tnt contrast (docs/TODO.md): ``tnt_open_anchor_contrast`` is an OOS-only,
  read-only trend-bucket comparison sourced from the Obsidian 做T research note
  (``clipper/2026-08-15-bollinger-volatility-t-strategy-research.md``); scripts
  listed under ``scrpits/tnt/`` are missing_not_in_repository and are NOT
  reproduced here. Diagnosis only: it never re-scans trades nor feeds back into
  the filter mask.
- ``trend_bucket``/``volatility_bucket`` are execution-day post-entry
  diagnostics from the planned execution day's complete daily bar; they never
  feed precheck, retention, or engine inputs.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from typing import Any, Mapping, Sequence

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig

FACTOR_ID = "daily_open_anchor_filter_v1"
SCHEMA_VERSION = 2
DISCLAIMER = "研究对照输出：仅统计性执行结果，不含交易指令、买卖方向或投资建议"

MAX_SYMBOLS_PER_REQUEST = 200
MAX_WINDOW_TRADING_DAYS = 370
MAX_SIGNALS_PER_REQUEST = 1000
ANCHOR_MAX_AGE_TRADING_DAYS = 20
MAX_HOLD_DAYS = 15
REQUIRED_HORIZON_DAYS = MAX_HOLD_DAYS + 1  # T+1 .. T+16 market days
MIN_OOS_TRADES = 30
EXECUTION_LEDGER_VERSION = 3

STOP_LOSS_PCT = -0.06
ENTRY_FILL = "open_t+1"
EXIT_FILL = "close_t"
FEES_PCT = 0.0002
SLIPPAGE_BPS = 5.0
STAMP_TAX_PCT = 0.0005

SINGLE_SIDE_BODY_RATIO = 0.60
VOLATILITY_BASELINE_DAYS = 20
VOLATILITY_HIGH_RATIO = 1.50
VOLATILITY_LOW_RATIO = 0.75
NEAR_ANCHOR_PCT = 0.02
FAR_ANCHOR_PCT = 0.05

ARMS = ("none", "original", "inverted", "random")

UPPER_MISMATCH_TOLERANCE = 0.005

TREND_CONTRAST_SOURCE = "obsidian-note/clipper/2026-08-15-bollinger-volatility-t-strategy-research.md"
TREND_CONTRAST_READ_SCOPE = "oos_only"
TREND_CONTRAST_PREREGISTERED = (
    "Obsidian 做T研究笔记（布林带+波动率，2026-08-15）已定结论：锚定开盘价的均值回归做T"
    "隐含均值回归假设，单边趋势日（尤其单边下跌）为接飞刀；过滤器有效性必须按趋势状态分层核对"
)
TREND_CONTRAST_PROXY_NOTE = (
    "日频降档代理：对照只读 OOS 执行日单边形态桶（planned execution day 完整日线的 body_ratio "
    "诊断，post-entry），不是对该笔记所述盘中做T研究的复现或等效替代"
)
TREND_CONTRAST_HISTORICAL_ARTIFACTS: tuple[dict[str, str], ...] = (
    {"path": "scrpits/tnt/fetch_minutes.py", "status": "missing_not_in_repository"},
    {"path": "scrpits/tnt/mean_revert_screen.py", "status": "missing_not_in_repository"},
    {"path": "scrpits/tnt/t_backtest_v4.py", "status": "missing_not_in_repository"},
    {"path": "scrpits/tnt/t_backtest_v6.py", "status": "missing_not_in_repository"},
)
TREND_CONTRAST_HISTORICAL_ARTIFACTS_NOTE = (
    "以上为笔记「可复用资产」所列脚本原文（含 scrpits 拼写）；仓库中不存在，对照仅引用笔记结论，不是代码复现"
)
TREND_CONTRAST_REGIMES = ("single_side_down", "range")

TREND_SHAPES = ("single_side_up", "single_side_down", "range", "unavailable_shape")
VOLATILITY_BUCKETS = ("high_volatility", "normal_volatility", "low_volatility", "insufficient_history")

REGIME_RATIOS: dict[str, Decimal] = {
    "main_10": Decimal("0.10"),
    "st_5": Decimal("0.05"),
    "chinext_20": Decimal("0.20"),
    "star_20": Decimal("0.20"),
    "beijing_30": Decimal("0.30"),
}

REQUIRED_CANONICAL_METHODS = (
    "generation",
    "manifest_sha256",
    "manifest",
    "market_days",
    "daily_bars",
    "columns",
)
REQUIRED_CANONICAL_COLUMNS = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
)

_FACT_KEYS = (
    "pre_close",
    "published_limit_up",
    "regime",
    "is_st",
    "name",
    "suspended",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
)
DEFINITION: dict[str, Any] = {
    "id": FACTOR_ID,
    "signal": "ma5_cross_above_ma20_close_t",
    "exit": "ma5_cross_below_ma20_or_stop_loss_-0.06_or_max_hold_15",
    "anchor": "latest_bullish_adjusted_open_strictly_before_signal_within_20_market_days",
    "arms": list(ARMS),
    "decision_basis": "close_t_vs_anchor",
    "entry_fill": ENTRY_FILL,
    "exit_fill": EXIT_FILL,
    "limit_band_basis": "raw_prev_close_plus_regime_round_half_up_cross_checked_with_published_limit_up",
    "random_seed": "sha256(symbol|signal_date)[:8]",
    "markets_pin": "canonical_manifest.source_generations['markets']",
    "horizon_market_days": REQUIRED_HORIZON_DAYS,
    "execution_day_diagnostics": {
        "basis": "planned_execution_day_complete_daily_bar_only",
        "scope": "post_entry_diagnosis_read_only_never_feeds_precheck_retention_or_engine",
        "trend_bucket": "body_ratio=(close-open)/(high-low); nonpositive span or invalid OHLC unavailable_shape; >0.60 or isclose(0.60,abs_tol=1e-12) single_side_up; <-0.60 or isclose(-0.60,abs_tol=1e-12) single_side_down; else range",
        "volatility_bucket": "true_range_pct=max(high-low,abs(high-prev_close),abs(low-prev_close))/prev_close; median of 20 prior complete market days; ratio >=1.50 high, <=0.75 low, else normal; missing history insufficient_history",
    },
}


LIMITS: dict[str, int] = {
    "max_symbols": MAX_SYMBOLS_PER_REQUEST,
    "max_window_trading_days": MAX_WINDOW_TRADING_DAYS,
    "max_signals": MAX_SIGNALS_PER_REQUEST,
    "anchor_max_age_trading_days": ANCHOR_MAX_AGE_TRADING_DAYS,
}

DATA_GATES: dict[str, str] = {
    "intraday": "unavailable_sealed_minute_coverage_insufficient",
}

STATS_KEYS = (
    "n_signals",
    "n_retained",
    "n_filtered",
    "n_candidates_executed",
    "n_trades",
    "stop_hit_count",
    "stop_hit_rate",
    "win_rate",
    "avg_win",
    "avg_loss",
    "payoff_ratio",
    "expectancy",
    "avg_mae",
    "avg_mfe",
    "net_pnl_pct_mean",
    "exit_reason_counts",
    "blocked_counts",
    "censored_counts",
)

PRECHECK_BLOCKED = "blocked"
PRECHECK_CENSORED = "censored"
PRECHECK_OK = "ok"


class UnavailableError(Exception):
    """Whole-request data-gate failure (fail-closed)."""

    def __init__(self, reason: str, **detail: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _finite_price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _finite_ratio(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _fact_get(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def rebuild_limit_bands(regime: Any, pre_close: Any) -> tuple[float | None, float | None]:
    """ROUND_HALF_UP band rebuild on the raw price scale (final-design §1.1)."""
    ratio = REGIME_RATIOS.get(regime) if isinstance(regime, str) else None
    base = _finite_price(pre_close)
    if ratio is None or base is None:
        return None, None
    amount = Decimal(str(base))
    upper = float((amount * (Decimal("1") + ratio)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    lower = float((amount * (Decimal("1") - ratio)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return upper, lower


def random_anchor_index(symbol: str, signal_date: date, window_size: int) -> int:
    digest = hashlib.sha256(f"{symbol}|{signal_date.isoformat()}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % window_size


@dataclass
class SymbolSeries:
    symbol: str
    dates: list[date] = field(default_factory=list)
    day_index: dict[date, int] = field(default_factory=dict)
    adj_open: dict[date, float] = field(default_factory=dict)
    adj_high: dict[date, float] = field(default_factory=dict)
    adj_low: dict[date, float] = field(default_factory=dict)
    adj_close: dict[date, float] = field(default_factory=dict)
    raw_open: dict[date, float] = field(default_factory=dict)
    raw_high: dict[date, float] = field(default_factory=dict)
    raw_low: dict[date, float] = field(default_factory=dict)
    raw_close: dict[date, float] = field(default_factory=dict)
    golden: set[date] = field(default_factory=set)
    dead: set[date] = field(default_factory=set)


def _golden_dead_crosses(closes: Sequence[float]) -> tuple[set[int], set[int]]:
    golden: set[int] = set()
    dead: set[int] = set()
    ma5: list[float | None] = [None] * len(closes)
    ma20: list[float | None] = [None] * len(closes)
    for index in range(len(closes)):
        if index + 1 >= 5:
            ma5[index] = sum(closes[index - 4 : index + 1]) / 5.0
        if index + 1 >= 20:
            ma20[index] = sum(closes[index - 19 : index + 1]) / 20.0
        if index == 0:
            continue
        prev_fast, fast = ma5[index - 1], ma5[index]
        prev_slow, slow = ma20[index - 1], ma20[index]
        if None in (prev_fast, fast, prev_slow, slow):
            continue
        if prev_fast <= prev_slow and fast > slow:
            golden.add(index)
        if prev_fast >= prev_slow and fast < slow:
            dead.add(index)
    return golden, dead


def _build_symbol_series(
    canonical: Any, symbol: str, load_start: date, load_end: date
) -> tuple[SymbolSeries | None, str | None]:
    frame = canonical.daily_bars(symbol, load_start, load_end)
    if frame is None or frame.is_empty():
        return None, "no_data"
    missing = [column for column in REQUIRED_CANONICAL_COLUMNS if column not in frame.columns]
    if missing:
        return None, "raw_field_missing"
    frame = frame.sort("date")
    series = SymbolSeries(symbol=symbol)
    closes: list[float] = []
    for row in frame.iter_rows(named=True):
        day = row["date"]
        if isinstance(day, str):
            day = date.fromisoformat(day)
        adjusted = [_finite_price(row[name]) for name in ("open", "high", "low", "close")]
        if any(value is None for value in adjusted):
            return None, "adjusted_field_invalid"
        raw_values = [_finite_price(row[f"raw_{name}"]) for name in ("open", "high", "low", "close")]
        series.dates.append(day)
        series.day_index[day] = len(series.dates) - 1
        series.adj_open[day], series.adj_high[day], series.adj_low[day], series.adj_close[day] = adjusted
        series.raw_open[day], series.raw_high[day], series.raw_low[day], series.raw_close[day] = raw_values
        closes.append(adjusted[3])
    golden, dead = _golden_dead_crosses(closes)
    series.golden = {series.dates[index] for index in golden}
    series.dead = {series.dates[index] for index in dead}
    return series, None


@dataclass(frozen=True)
class AnchorInfo:
    day: date
    value: float
    age_trading_days: int
    basis: str = "open_adjusted"


@dataclass
class Candidate:
    symbol: str
    signal_date: date
    segment: str
    day_position: int
    anchor: AnchorInfo | None = None
    random_anchor: AnchorInfo | None = None
    close_t: float = 0.0
    precheck: str = PRECHECK_OK
    planned_execution_date: date | None = None
    trend_bucket: str = "unknown"
    volatility_bucket: str = "unknown"
    gap_bucket: str = "unknown"
    distance_bucket: str = "unknown"
    retained: dict[str, bool | None] = field(default_factory=dict)

@dataclass
class LedgerEntry:
    symbol: str
    signal_date: date
    arm: str
    segment: str
    planned_execution_date: date | None
    filter_retained: bool | None
    precheck: str
    terminal_status: str  # traded | blocked | censored | not_retained
    terminal_reason: str | None = None
    exit_reason: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    pnl_pct: float | None = None
    mae_pct: float | None = None
    mfe_pct: float | None = None
    blocked_exit_days: int = 0
    engine_entry_index: int | None = None
    trend_bucket: str = "unknown"
    gap_bucket: str = "unknown"
    distance_bucket: str = "unknown"
    volatility_bucket: str = "unknown"


class _FactsView:
    """Raw-scale band rebuild + cross-check against the published limit-up price."""

    def __init__(self, rows: Mapping[tuple[str, date], Mapping[str, Any]]) -> None:
        self._rows = rows
        self._cache: dict[tuple[str, date], tuple[float, float]] = {}

    def fact(self, symbol: str, day: date) -> Mapping[str, Any]:
        row = self._rows.get((symbol, day))
        if row is None:
            raise UnavailableError(
                "limit_band_facts_incomplete", symbol=symbol, date=day.isoformat()
            )
        return row

    def band(self, symbol: str, day: date) -> tuple[float, float]:
        key = (symbol, day)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        row = self.fact(symbol, day)
        regime = _fact_get(row, "regime")
        if regime not in REGIME_RATIOS:
            raise UnavailableError(
                "limit_band_facts_incomplete", symbol=symbol, date=day.isoformat(), field="regime"
            )
        published_upper = _finite_price(_fact_get(row, "published_limit_up"))
        if published_upper is None:
            raise UnavailableError(
                "published_upper_missing", symbol=symbol, date=day.isoformat()
            )
        pre_close = _finite_price(_fact_get(row, "pre_close"))
        if pre_close is None:
            # 契约：markets pre_close 缺失即整单 unavailable，禁止用相邻 close 猜测。
            raise UnavailableError(
                "limit_band_facts_incomplete", symbol=symbol, date=day.isoformat(), field="pre_close"
            )
        upper, lower = rebuild_limit_bands(regime, pre_close)
        if upper is None or lower is None:
            raise UnavailableError(
                "limit_band_facts_incomplete", symbol=symbol, date=day.isoformat(), field="pre_close"
            )
        if abs(upper - published_upper) > UPPER_MISMATCH_TOLERANCE:
            raise UnavailableError(
                "published_upper_mismatch",
                symbol=symbol,
                date=day.isoformat(),
                rebuilt_upper=upper,
                published=published_upper,
            )
        self._cache[key] = (upper, lower)
        return upper, lower


def _anchor_for_signal(
    series: SymbolSeries,
    market_days: Sequence[date],
    day_position: int,
    signal_date: date,
) -> AnchorInfo | None:
    lower_bound = max(0, day_position - ANCHOR_MAX_AGE_TRADING_DAYS)
    for position in range(day_position - 1, lower_bound - 1, -1):
        day = market_days[position]
        if day not in series.day_index:
            continue
        if series.adj_close[day] > series.adj_open[day]:
            age = day_position - position
            return AnchorInfo(day=day, value=series.adj_open[day], age_trading_days=age)
    return None


def _random_anchor_for_signal(
    symbol: str,
    series: SymbolSeries,
    market_days: Sequence[date],
    day_position: int,
    signal_date: date,
) -> AnchorInfo | None:
    lower_bound = max(0, day_position - ANCHOR_MAX_AGE_TRADING_DAYS)
    window = [
        market_days[position]
        for position in range(day_position - 1, lower_bound - 1, -1)
        if market_days[position] in series.day_index
    ]
    if not window:
        return None
    picked = window[random_anchor_index(symbol, signal_date, len(window))]
    age = day_position - series.day_index[picked]
    return AnchorInfo(day=picked, value=series.adj_open[picked], age_trading_days=age)


def execution_day_shape_bucket(open_: Any, high: Any, low: Any, close: Any) -> str:
    values = (open_, high, low, close)
    if any(value is None or isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return "unavailable_shape"
    numbers = [float(value) for value in values]
    if not all(math.isfinite(value) for value in numbers) or any(value <= 0 for value in numbers):
        return "unavailable_shape"
    body_open, body_high, body_low, body_close = numbers
    span = body_high - body_low
    if span <= 0 or not (
        body_low <= body_open <= body_high and body_low <= body_close <= body_high
    ):
        return "unavailable_shape"
    body_ratio = (body_close - body_open) / span
    if body_ratio > SINGLE_SIDE_BODY_RATIO or math.isclose(
        body_ratio, SINGLE_SIDE_BODY_RATIO, rel_tol=0.0, abs_tol=1e-12
    ):
        return "single_side_up"
    if body_ratio < -SINGLE_SIDE_BODY_RATIO or math.isclose(
        body_ratio, -SINGLE_SIDE_BODY_RATIO, rel_tol=0.0, abs_tol=1e-12
    ):
        return "single_side_down"
    return "range"


def true_range_pct(high: Any, low: Any, close: Any, prev_close: Any) -> float | None:
    values = (high, low, close, prev_close)
    if any(value is None or isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return None
    numbers = [float(value) for value in values]
    if not all(math.isfinite(value) for value in numbers) or any(value <= 0 for value in numbers):
        return None
    bar_high, bar_low, bar_close = numbers[0], numbers[1], numbers[2]
    if bar_high < bar_low or not (bar_low <= bar_close <= bar_high):
        return None
    prev = numbers[3]
    return max(bar_high - bar_low, abs(bar_high - prev), abs(bar_low - prev)) / prev


def volatility_bucket_from_tr(current: float | None, baseline: Sequence[float | None]) -> str:
    if current is None or len(baseline) != VOLATILITY_BASELINE_DAYS:
        return "insufficient_history"
    values: list[float] = []
    for value in baseline:
        if value is None or not math.isfinite(value):
            return "insufficient_history"
        values.append(float(value))
    median_tr = median(values)
    if median_tr <= 0:
        return "insufficient_history"
    ratio = current / median_tr
    if ratio >= VOLATILITY_HIGH_RATIO:
        return "high_volatility"
    if ratio <= VOLATILITY_LOW_RATIO:
        return "low_volatility"
    return "normal_volatility"


def _bucket_execution_trend(series: SymbolSeries, market_days: Sequence[date], planned_position: int) -> str:
    if planned_position < 0 or planned_position >= len(market_days):
        return "unavailable_shape"
    day = market_days[planned_position]
    return execution_day_shape_bucket(series.adj_open.get(day), series.adj_high.get(day), series.adj_low.get(day), series.adj_close.get(day))


def _bucket_execution_volatility(series: SymbolSeries, market_days: Sequence[date], planned_position: int) -> str:
    position = planned_position
    first_baseline = position - VOLATILITY_BASELINE_DAYS
    if first_baseline < 1 or position >= len(market_days):
        return "insufficient_history"
    execution_day = market_days[position]
    current = true_range_pct(
        series.adj_high.get(execution_day),
        series.adj_low.get(execution_day),
        series.adj_close.get(execution_day),
        series.adj_close.get(market_days[position - 1]),
    )
    baseline = [
        true_range_pct(
            series.adj_high.get(market_days[index]),
            series.adj_low.get(market_days[index]),
            series.adj_close.get(market_days[index]),
            series.adj_close.get(market_days[index - 1]),
        )
        for index in range(first_baseline, position)
    ]
    return volatility_bucket_from_tr(current, baseline)


def _bucket_gap(series: SymbolSeries, execution_day: date | None, signal_date: date) -> str:
    if execution_day is None:
        return "unknown"
    open_t1 = series.adj_open.get(execution_day)
    if open_t1 is None:
        return "unknown"
    delta = open_t1 - series.adj_close[signal_date]
    if delta > 0:
        return "gap_up"
    if delta < 0:
        return "gap_down"
    return "flat"


def _bucket_distance(anchor: AnchorInfo | None, close_t: float) -> str:
    if anchor is None or anchor.value <= 0:
        return "unknown"
    distance = abs(close_t / anchor.value - 1.0)
    if distance < NEAR_ANCHOR_PCT:
        return "near_anchor"
    if distance < FAR_ANCHOR_PCT:
        return "mid_anchor"
    return "far_anchor"


def _retention_map(anchor: AnchorInfo | None, random_anchor: AnchorInfo | None, close_t: float) -> dict[str, bool | None]:
    if anchor is None:
        return {"none": True, "original": None, "inverted": None, "random": None}
    return {
        "none": True,
        "original": close_t < anchor.value,
        "inverted": close_t >= anchor.value,
        "random": close_t < random_anchor.value if random_anchor is not None else None,
    }


def _precheck_candidate(
    series: SymbolSeries,
    market_days: Sequence[date],
    day_position: int,
    facts: _FactsView,
) -> tuple[str, date | None, list[date]]:
    """Return (status, T+1 date, horizon day list); bands validate facts fail-closed."""
    if day_position + 1 >= len(market_days):
        return "censored:next_market_day_unavailable", None, []
    t1 = market_days[day_position + 1]
    if t1 not in series.day_index:
        return "censored:t1_bar_missing", t1, []
    horizon: list[date] = []
    for offset in range(1, REQUIRED_HORIZON_DAYS + 1):
        if day_position + offset >= len(market_days):
            return "censored:horizon_data_gap", t1, horizon
        day = market_days[day_position + offset]
        if day not in series.day_index:
            return "censored:horizon_data_gap", t1, horizon
        horizon.append(day)
    if series.raw_open.get(t1) is None:
        return "censored:invalid_open", t1, []
    upper_t1, _ = facts.band(series.symbol, t1)
    raw_open = series.raw_open[t1]
    raw_low = series.raw_low[t1]
    if raw_open >= upper_t1 and raw_low >= upper_t1:
        return "blocked:buy_limit_up", t1, horizon
    return PRECHECK_OK, t1, horizon


def _candidate_panel(
    candidate: Candidate,
    series: SymbolSeries,
    market_days: Sequence[date],
    day_position: int,
    facts: _FactsView,
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    days = [market_days[day_position]] + [
        market_days[day_position + offset] for offset in range(1, REQUIRED_HORIZON_DAYS + 1)
    ]
    for day in days:
        if series.raw_high.get(day) is None or series.raw_low.get(day) is None:
            raise UnavailableError(
                "limit_band_facts_incomplete",
                symbol=candidate.symbol,
                date=day.isoformat(),
                field="raw_high_or_raw_low",
            )
        upper, lower = facts.band(candidate.symbol, day)
        rows.append(
            {
                "symbol": candidate.symbol,
                "date": day,
                "open": series.adj_open[day],
                "high": series.adj_high[day],
                "low": series.adj_low[day],
                "close": series.adj_close[day],
                "signal_limit_up": series.raw_high[day] >= upper,
                "signal_limit_down": series.raw_low[day] <= lower,
            }
        )
    return pl.DataFrame(rows)


def _run_engine(panel: pl.DataFrame, entries: pl.Series, exits: pl.Series, config: MatcherConfig):
    engine = BacktestEngine(None)
    return engine.simulate_independent_candidates(panel, entries, exits, config)


def _matcher_config() -> MatcherConfig:
    return MatcherConfig(
        entry_fill=ENTRY_FILL,
        exit_fill=EXIT_FILL,
        fees_pct=FEES_PCT,
        slippage_bps=SLIPPAGE_BPS,
        stamp_tax_pct=STAMP_TAX_PCT,
        stop_loss_pct=STOP_LOSS_PCT,
        max_hold_days=MAX_HOLD_DAYS,
    )


_BUY_BLOCK_KEYS = ("buy_limit_up", "buy_suspended", "buy_score_filter", "buy_volume_cap", "buy_no_next_bar")


def _terminal_from_result(
    result: Any, candidate: Candidate, arm: str, planned: date | None, engine_entry_index: int | None
) -> LedgerEntry:
    entry = LedgerEntry(
        symbol=candidate.symbol,
        signal_date=candidate.signal_date,
        arm=arm,
        segment=candidate.segment,
        planned_execution_date=planned,
        filter_retained=True,
        precheck=candidate.precheck,
        terminal_status="blocked",
        engine_entry_index=engine_entry_index,
        trend_bucket=candidate.trend_bucket,
        volatility_bucket=candidate.volatility_bucket,
        gap_bucket=candidate.gap_bucket,
        distance_bucket=candidate.distance_bucket,
    )
    trades = list(result.trades)
    if len(trades) > 1:
        raise UnavailableError(
            "engine_ledger_inconsistent",
            symbol=candidate.symbol,
            signal_date=candidate.signal_date.isoformat(),
            arm=arm,
            trades=len(trades),
        )
    if len(trades) == 1:
        trade = trades[0]
        if str(trade.entry_signal_date) != candidate.signal_date.isoformat():
            raise UnavailableError(
                "engine_ledger_inconsistent",
                symbol=candidate.symbol,
                signal_date=candidate.signal_date.isoformat(),
                arm=arm,
                field="entry_signal_date",
            )
        entry.terminal_status = "traded"
        entry.exit_reason = str(trade.exit_reason)
        entry.entry_price = float(trade.entry_price)
        entry.exit_price = float(trade.exit_price)
        entry.pnl_pct = float(trade.pnl_pct)
        entry.mae_pct = float(trade.mae_pct) if trade.mae_pct is not None else None
        entry.mfe_pct = float(trade.mfe_pct) if trade.mfe_pct is not None else None
        entry.blocked_exit_days = int(trade.blocked_exit_days)
        return entry
    execution = dict(result.stats.get("execution") or {})
    if any(int(execution.get(key) or 0) > 0 for key in _BUY_BLOCK_KEYS):
        raise UnavailableError(
            "engine_ledger_inconsistent",
            symbol=candidate.symbol,
            signal_date=candidate.signal_date.isoformat(),
            arm=arm,
            execution=execution,
        )
    sell_suspended = int(execution.get("sell_suspended") or 0)
    sell_limit_down = int(execution.get("sell_limit_down") or 0)
    entry.blocked_exit_days = sell_suspended + sell_limit_down
    for reason, count in (("sell_suspended", sell_suspended), ("sell_limit_down", sell_limit_down)):
        if count > 0:
            entry.terminal_reason = reason
            return entry
    raise UnavailableError(
        "engine_ledger_inconsistent",
        symbol=candidate.symbol,
        signal_date=candidate.signal_date.isoformat(),
        arm=arm,
        execution=execution,
    )


def _ledger_for_candidate(
    candidate: Candidate,
    series: SymbolSeries,
    market_days: Sequence[date],
    day_position: int,
    facts: _FactsView,
) -> list[LedgerEntry]:
    planned = candidate.planned_execution_date
    entries: list[LedgerEntry] = []
    if candidate.precheck == PRECHECK_OK:
        panel = _candidate_panel(candidate, series, market_days, day_position, facts)
        entries_series = pl.Series("entries", [day == candidate.signal_date for day in panel["date"].to_list()])
        exits_series = pl.Series("exits", [day in series.dead for day in panel["date"].to_list()])
        for arm in ARMS:
            retained = candidate.retained.get(arm)
            if retained is None:
                entries.append(
                    LedgerEntry(
                        symbol=candidate.symbol,
                        signal_date=candidate.signal_date,
                        arm=arm,
                        segment=candidate.segment,
                        planned_execution_date=planned,
                        filter_retained=None,
                        precheck=candidate.precheck,
                        terminal_status="censored",
                        terminal_reason="anchor_unavailable",
                        trend_bucket=candidate.trend_bucket,
                        volatility_bucket=candidate.volatility_bucket,
                        gap_bucket=candidate.gap_bucket,
                        distance_bucket=candidate.distance_bucket,
                    )
                )
                continue
            if retained is not True:
                entries.append(
                    LedgerEntry(
                        symbol=candidate.symbol,
                        signal_date=candidate.signal_date,
                        arm=arm,
                        segment=candidate.segment,
                        planned_execution_date=planned,
                        filter_retained=False,
                        precheck=candidate.precheck,
                        terminal_status="not_retained",
                        terminal_reason="arm_filtered",
                        trend_bucket=candidate.trend_bucket,
                        volatility_bucket=candidate.volatility_bucket,
                        gap_bucket=candidate.gap_bucket,
                        distance_bucket=candidate.distance_bucket,
                    )
                )
                continue
            result = _run_engine(panel, entries_series, exits_series, _matcher_config())
            entry = _terminal_from_result(result, candidate, arm, planned, engine_entry_index=1)
            entries.append(entry)
        return entries
    status, reason = candidate.precheck.split(":", 1)
    for arm in ARMS:
        retained = candidate.retained.get(arm)
        if retained is None:
            continue
        entries.append(
            LedgerEntry(
                symbol=candidate.symbol,
                signal_date=candidate.signal_date,
                arm=arm,
                segment=candidate.segment,
                planned_execution_date=planned,
                filter_retained=bool(retained),
                precheck=candidate.precheck,
                terminal_status=status,
                terminal_reason=reason,
                trend_bucket=candidate.trend_bucket,
                volatility_bucket=candidate.volatility_bucket,
                gap_bucket=candidate.gap_bucket,
                distance_bucket=candidate.distance_bucket,
            )
        )
    return entries


def _round6(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _segment_stats(n_signals: int, entries: Sequence[LedgerEntry]) -> dict[str, Any]:
    pnls = [entry.pnl_pct for entry in entries if entry.terminal_status == "traded" and entry.pnl_pct is not None]
    n_trades = len(pnls)
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    stop_hit = sum(1 for entry in entries if entry.exit_reason == "stop_loss")
    exit_reasons = Counter(entry.exit_reason for entry in entries if entry.exit_reason)
    blocked = Counter(entry.terminal_reason or entry.precheck for entry in entries if entry.terminal_status == "blocked")
    censored = Counter(
        (entry.terminal_reason or entry.precheck.split(":", 1)[-1])
        for entry in entries
        if entry.terminal_status == "censored"
    )
    return {
        "n_signals": n_signals,
        "n_retained": sum(1 for entry in entries if entry.filter_retained is True),
        "n_filtered": sum(1 for entry in entries if entry.filter_retained is False),
        "n_candidates_executed": sum(1 for entry in entries if entry.engine_entry_index is not None),
        "n_trades": n_trades,
        "stop_hit_count": stop_hit,
        "stop_hit_rate": _round6(stop_hit / n_trades) if n_trades else None,
        "win_rate": _round6(len(wins) / n_trades) if n_trades else None,
        "avg_win": _round6(avg_win),
        "avg_loss": _round6(avg_loss),
        "payoff_ratio": _round6(avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0) else None,
        "expectancy": _round6(sum(pnls) / n_trades) if n_trades else None,
        "avg_mae": _round6(
            sum(entry.mae_pct for entry in entries if entry.mae_pct is not None)
            / sum(1 for entry in entries if entry.mae_pct is not None)
        )
        if any(entry.mae_pct is not None for entry in entries)
        else None,
        "avg_mfe": _round6(
            sum(entry.mfe_pct for entry in entries if entry.mfe_pct is not None)
            / sum(1 for entry in entries if entry.mfe_pct is not None)
        )
        if any(entry.mfe_pct is not None for entry in entries)
        else None,
        "net_pnl_pct_mean": _round6(sum(pnls) / n_trades) if n_trades else None,
        "exit_reason_counts": dict(exit_reasons),
        "blocked_counts": dict(blocked),
        "censored_counts": dict(censored),
    }


def _segment_layers(entries: Sequence[LedgerEntry]) -> dict[str, Any]:
    layers: dict[str, dict[str, dict[str, Any]]] = {}
    for name, getter in (
        ("trend_bucket", lambda entry: entry.trend_bucket),
        ("volatility_bucket", lambda entry: entry.volatility_bucket),
        ("gap_bucket", lambda entry: entry.gap_bucket),
        ("anchor_distance_bucket", lambda entry: entry.distance_bucket),
    ):
        buckets: dict[str, dict[str, Any]] = {}
        grouped: dict[str, list[LedgerEntry]] = {}
        for entry in entries:
            grouped.setdefault(getter(entry), []).append(entry)
        for bucket, bucket_entries in sorted(grouped.items()):
            pnls = [entry.pnl_pct for entry in bucket_entries if entry.pnl_pct is not None]
            n_trades = len(pnls)
            stop_hit = sum(1 for entry in bucket_entries if entry.exit_reason == "stop_loss")
            mean_pnl = _round6(sum(pnls) / n_trades) if n_trades else None
            buckets[bucket] = {
                "n": len(bucket_entries),
                "n_trades": n_trades,
                "net_pnl_pct_mean": mean_pnl,
                "expectancy": mean_pnl,
                "stop_hit_rate": _round6(stop_hit / n_trades) if n_trades else None,
                "blocked": sum(1 for entry in bucket_entries if entry.terminal_status == "blocked"),
                "censored": sum(1 for entry in bucket_entries if entry.terminal_status == "censored"),
            }
        layers[name] = buckets
    return layers


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _trend_bucket_stats(arms: dict[str, Any], arm: str, bucket: str) -> Mapping[str, Any] | None:
    """Read-only OOS access to one arm's trend bucket."""
    segments = arms.get(arm, {})
    oos = segments.get("segments", {}).get("oos", {}) if isinstance(segments, Mapping) else {}
    layers = oos.get("layers", {}) if isinstance(oos, Mapping) else {}
    buckets = layers.get("trend_bucket", {}) if isinstance(layers, Mapping) else {}
    stats = buckets.get(bucket) if isinstance(buckets, Mapping) else None
    return stats if isinstance(stats, Mapping) else None


def _contrast_arm_view(stats: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(stats, Mapping):
        return {"n_trades": None, "stop_hit_rate": None, "expectancy": None}
    return {
        "n_trades": stats.get("n_trades"),
        "stop_hit_rate": stats.get("stop_hit_rate"),
        "expectancy": stats.get("expectancy"),
    }


def _contrast_bucket_status(
    none_stats: Mapping[str, Any] | None, original_stats: Mapping[str, Any] | None
) -> str:
    """Apply the frozen OOS-only trend contrast decision rule."""
    metrics: list[tuple[float, float]] = []
    for stats in (none_stats, original_stats):
        if not isinstance(stats, Mapping):
            return "inconclusive"
        n_trades = stats.get("n_trades")
        stop_hit_rate = stats.get("stop_hit_rate")
        expectancy = stats.get("expectancy")
        if (
            not _finite_number(n_trades)
            or int(n_trades) < MIN_OOS_TRADES
            or not _finite_number(stop_hit_rate)
            or not _finite_number(expectancy)
        ):
            return "inconclusive"
        metrics.append((float(stop_hit_rate), float(expectancy)))
    none_stop, none_expectancy = metrics[0]
    original_stop, original_expectancy = metrics[1]
    if original_stop > none_stop or original_expectancy < none_expectancy:
        return "adverse"
    if original_stop < none_stop and original_expectancy >= none_expectancy:
        return "improved"
    return "neutral"


def build_tnt_open_anchor_contrast(arms: dict[str, Any]) -> dict[str, Any]:
    """Build the Obsidian-sourced tnt trend contrast from existing OOS layers only.

    This is read-only diagnosis: it does not rescan trades or feed back into
    the filter mask.
    """
    regimes: dict[str, Any] = {}
    for bucket in TREND_CONTRAST_REGIMES:
        none_stats = _trend_bucket_stats(arms, "none", bucket)
        original_stats = _trend_bucket_stats(arms, "original", bucket)
        regimes[bucket] = {
            "none": _contrast_arm_view(none_stats),
            "original": _contrast_arm_view(original_stats),
            "status": _contrast_bucket_status(none_stats, original_stats),
        }
    return {
        "source": TREND_CONTRAST_SOURCE,
        "read_scope": TREND_CONTRAST_READ_SCOPE,
        "preregistered_conclusion": TREND_CONTRAST_PREREGISTERED,
        "proxy_note": TREND_CONTRAST_PROXY_NOTE,
        "historical_artifacts": [dict(item) for item in TREND_CONTRAST_HISTORICAL_ARTIFACTS],
        "historical_artifacts_note": TREND_CONTRAST_HISTORICAL_ARTIFACTS_NOTE,
        "decision_rule": {
            "min_oos_trades_per_arm": MIN_OOS_TRADES,
            "inconclusive": "任一臂 n_trades < min_oos_trades_per_arm 或指标缺失/非有限值",
            "adverse": "original stop_hit_rate 高于 none，或 expectancy 低于 none",
            "improved": "original stop_hit_rate 低于 none 且 expectancy 不低于 none",
            "neutral": "其余情形",
        },
        "regimes": regimes,
    }


def _verdict(arms: dict[str, Any], contrast: dict[str, Any] | None = None) -> dict[str, Any]:
    oos_none = arms["none"]["segments"]["oos"]["stats"]
    oos_original = arms["original"]["segments"]["oos"]["stats"]
    enough = oos_none["n_trades"] >= MIN_OOS_TRADES and oos_original["n_trades"] >= MIN_OOS_TRADES
    if not enough:
        label = "inconclusive"
    else:
        stop_ok = oos_original["stop_hit_rate"] < oos_none["stop_hit_rate"]
        expectancy_ok = oos_original["expectancy"] >= oos_none["expectancy"]
        label = "validated" if stop_ok and expectancy_ok else "rejected"
    if contrast is None:
        contrast = build_tnt_open_anchor_contrast(arms)
    down_status = contrast["regimes"]["single_side_down"]["status"]
    range_status = contrast["regimes"]["range"]["status"]
    warnings: list[str] = []
    if label == "rejected":
        applicability = "not_applicable_rejected"
    elif label == "inconclusive":
        applicability = "inconclusive_overall"
    elif down_status == "inconclusive" or range_status == "inconclusive":
        applicability = "inconclusive_by_trend"
        warnings.append(
            "单边下跌与/或震荡趋势对照因样本不足不可判定"
            f"（任一臂 OOS n_trades < {MIN_OOS_TRADES}）：无法按趋势状态分层下结论"
        )
    elif down_status == "adverse" and range_status == "adverse":
        applicability = "unsupported_in_preregistered_regimes"
        warnings.append(
            "单边下跌与震荡两个趋势桶均不利"
            "（original 相对 none 止损触发率更高或期望更低），与 Obsidian 做T研究笔记预注册的"
            "单边趋势日接飞刀结论一致：validated 不适用于这两类趋势状态"
        )
    elif down_status == "adverse":
        applicability = "conditional_by_trend"
        warnings.append(
            "单边下跌趋势桶中 original 相对 none 止损触发率更高或期望更低"
            "（Obsidian 做T研究笔记的接飞刀结论在 OOS 执行日日型分层中复现）：validated 结论仅适用于非单边下跌行情"
        )
    elif range_status == "adverse":
        applicability = "conditional_by_trend"
        warnings.append(
            "震荡趋势桶中 original 相对 none 止损触发率更高或期望更低"
            "：validated 结论不适用于震荡行情"
        )
    else:
        applicability = "all_regimes"
    return {
        "label": label,
        "basis": "oos",
        "rules": [
            f"min_oos_trades={MIN_OOS_TRADES}",
            "stop_hit_rate(original) < stop_hit_rate(none)",
            "expectancy(original) >= expectancy(none)",
        ],
        "applicability": applicability,
        "trend_contrast_statuses": {"single_side_down": down_status, "range": range_status},
        "warnings": warnings,
    }


def _unavailable_payload(reasons: Sequence[str], detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "factor_id": FACTOR_ID,
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "reasons": list(reasons),
        "detail": dict(detail or {}),
        "definition": DEFINITION,
        "disclaimer": DISCLAIMER,
    }


def unavailable_payload(reasons: Sequence[str], detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return _unavailable_payload(reasons, detail)


def assess_daily_open_anchor_capability(canonical: Any) -> dict[str, Any]:
    reasons: list[str] = []
    if canonical is None:
        reasons.append("canonical_reader_missing")
    else:
        for name in REQUIRED_CANONICAL_METHODS:
            if not callable(getattr(canonical, name, None)):
                reasons.append("canonical_reader_invalid")
                break
        else:
            try:
                columns = set(canonical.columns())
            except Exception:
                columns = set()
            missing = [column for column in REQUIRED_CANONICAL_COLUMNS if column not in columns]
            if missing:
                reasons.append("canonical_columns_missing")
    markets: dict[str, Any] = {
        "pin": None,
        "pin_manifest_sha256": None,
        "pin_verified": False,
        "opened": False,
        "provides": {},
        "reasons": [],
        "detail": {},
    }
    if not reasons and canonical is not None:
        reader = None
        try:
            reader, pin = _open_markets_reader(canonical)
            fields = _markets_field_support(reader)
            mode = getattr(reader, "pin_verification_mode", lambda: "unknown")()
            markets.update(
                {
                    "pin": pin,
                    "pin_manifest_sha256": reader.pin_manifest_sha256(),
                    "pin_verified": reader.pin_identity_verified(),
                    "pin_verification_mode": mode,
                    "opened": True,
                    "provides": fields,
                }
            )
            if not reader.pin_identity_verified():
                reason = "markets_pin_identity_unverified"
                reasons.append(reason)
                markets["reasons"].append(reason)
                markets["detail"] = {"mode": mode}
            else:
                missing = [name for name, supported in fields.items() if not supported]
                if missing:
                    reason = "markets_fact_fields_unsupported"
                    reasons.append(reason)
                    markets["reasons"].append(reason)
                    markets["detail"] = {"missing": missing}
        except UnavailableError as exc:
            reasons.append(exc.reason)
            markets["reasons"].append(exc.reason)
            markets["detail"] = dict(exc.detail)
        finally:
            if reader is not None:
                reader.close()
    return {
        "factor_id": FACTOR_ID,
        "schema_version": SCHEMA_VERSION,
        "available": not reasons,
        "reasons": reasons,
        "definition": DEFINITION,
        "limits": LIMITS,
        "data_gates": DATA_GATES,
        "markets_facts": markets,
        "disclaimer": DISCLAIMER,
    }


def capability_payload(canonical: Any) -> dict[str, Any]:
    return assess_daily_open_anchor_capability(canonical)


def resolve_daily_open_anchor_canonical(repo: Any) -> Any:
    canonical = getattr(repo, "generation_pinned_daily_reader", None)
    if callable(canonical):
        return canonical
    if canonical is not None:
        return canonical
    from app.services.research_sealed_data import PublishedCanonicalDailyReader

    try:
        return PublishedCanonicalDailyReader.from_repository(repo)
    except Exception:
        return None


def _open_markets_reader(canonical: Any) -> tuple[Any, str]:
    manifest = canonical.manifest()
    configured = (manifest.get("source_generations") or {}).get("markets")
    pin = configured.get("generation") if isinstance(configured, Mapping) else configured
    if not isinstance(pin, str) or not pin:
        raise UnavailableError("markets_pin_missing")
    try:
        from app.data_providers.fquant.daily_market_research import PublishedDailyMarketFactsReader
        reader = PublishedDailyMarketFactsReader.from_canonical_manifest(manifest)
    except ImportError as exc:
        raise UnavailableError("shared_market_facts_loader_missing") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise UnavailableError("markets_generation_unopenable", error=str(exc)) from exc
    if reader.generation() != pin:
        reader.close()
        raise UnavailableError("markets_generation_mismatch", pinned=pin, opened=reader.generation())
    return reader, pin


def _markets_field_support(reader: Any) -> dict[str, bool]:
    names = set(getattr(reader, "_column_names", set()) or set())
    payload = bool(getattr(reader, "_has_payload_json", False))
    quotes = getattr(reader, "_quote_columns", {}) or {}
    direct = getattr(reader, "_direct_fields", {}) or {}
    return {
        "raw": bool(payload or any(quotes.values())),
        "pre_close": bool(payload or "zrspj" in names),
        "regime": True,
        "ztj": bool(payload or direct.get("ztj", False)),
    }


def _resolve_markets_pin(canonical: Any) -> tuple[str, str]:
    reader, pin = _open_markets_reader(canonical)
    try:
        if not reader.pin_identity_verified():
            mode = getattr(reader, "pin_verification_mode", lambda: "unknown")()
            raise UnavailableError("markets_pin_identity_unverified", mode=mode)
        markets_hash = reader.pin_manifest_sha256()
        if not isinstance(markets_hash, str) or len(markets_hash) != 64:
            raise UnavailableError("manifest_identity_invalid")
        return pin, markets_hash
    finally:
        reader.close()


def _load_market_fact_rows(
    canonical: Any, symbols: Sequence[str], days: Sequence[date]
) -> dict[tuple[str, date], dict[str, Any]]:
    try:
        from app.data_providers.fquant.daily_market_research import load_pinned_market_facts
    except ImportError as exc:  # pragma: no cover - integration seam
        raise UnavailableError("shared_market_facts_loader_missing") from exc
    pin, markets_hash = _resolve_markets_pin(canonical)
    bundle = load_pinned_market_facts(canonical.manifest(), list(symbols), list(days))
    if bundle.generation != pin:
        raise UnavailableError(
            "markets_generation_mismatch", pinned=pin, opened=bundle.generation
        )
    if bundle.manifest_sha256 != markets_hash:
        raise UnavailableError("manifest_identity_invalid")
    rows: dict[tuple[str, date], dict[str, Any]] = {}
    for key, row in bundle.rows.items():
        rows[(key[0], key[1])] = {name: _fact_get(row, name) for name in _FACT_KEYS}
    return rows


def _validate_request(canonical: Any, start: date, end: date, oos_start: date, symbols: Sequence[str]) -> list[date]:
    if start > end:
        raise ValueError("start must be <= end")
    if not start <= oos_start <= end:
        raise ValueError("oos_start must be within [start, end]")
    unique = sorted(set(symbols))
    if not unique:
        raise ValueError("symbols must not be empty")
    if len(unique) > MAX_SYMBOLS_PER_REQUEST:
        raise ValueError(f"symbols must not exceed {MAX_SYMBOLS_PER_REQUEST}")
    window_days = canonical.market_days(start, end)
    if len(window_days) > MAX_WINDOW_TRADING_DAYS:
        raise ValueError(f"window must not exceed {MAX_WINDOW_TRADING_DAYS} trading days")
    return window_days


def evaluate_daily_open_anchor(
    canonical: Any,
    start: date,
    end: date,
    oos_start: date,
    symbols: Sequence[str],
) -> dict[str, Any]:
    """Run the four-arm research; ValueError → 400, UnavailableError → unavailable."""
    _validate_request(canonical, start, end, oos_start, symbols)
    unique_symbols = sorted(set(symbols))
    load_start = start - timedelta(days=45)
    load_end = end + timedelta(days=60)
    market_days = canonical.market_days(load_start, load_end)
    day_pos = {day: index for index, day in enumerate(market_days)}

    series_map: dict[str, SymbolSeries] = {}
    censored_symbols: list[dict[str, Any]] = []
    for symbol in unique_symbols:
        series, code = _build_symbol_series(canonical, symbol, load_start, load_end)
        if series is None:
            censored_symbols.append({"symbol": symbol, "code": code})
            continue
        series_map[symbol] = series

    signal_dates: list[tuple[str, date]] = []
    for symbol, series in series_map.items():
        for day in sorted(series.golden):
            if start <= day <= end:
                signal_dates.append((symbol, day))
    signal_dates.sort(key=lambda item: (item[1], item[0]))
    if len(signal_dates) > MAX_SIGNALS_PER_REQUEST:
        raise ValueError(f"signals must not exceed {MAX_SIGNALS_PER_REQUEST} per request")

    needed_days: set[date] = set()
    for symbol, day in signal_dates:
        position = day_pos.get(day)
        if position is None:
            continue
        needed_days.add(day)
        for offset in range(1, REQUIRED_HORIZON_DAYS + 1):
            if position + offset < len(market_days):
                needed_days.add(market_days[position + offset])
    markets_generation, markets_hash = _resolve_markets_pin(canonical)
    facts_rows: dict[tuple[str, date], dict[str, Any]] = {}
    if signal_dates and needed_days:
        facts_rows = _load_market_fact_rows(canonical, unique_symbols, sorted(needed_days))
    facts = _FactsView(facts_rows)

    candidates: list[Candidate] = []
    for symbol, signal_day in signal_dates:
        series = series_map[symbol]
        position = day_pos[signal_day]
        anchor = _anchor_for_signal(series, market_days, position, signal_day)
        random_anchor = (
            _random_anchor_for_signal(symbol, series, market_days, position, signal_day)
            if anchor is not None
            else None
        )
        close_t = series.adj_close[signal_day]
        if anchor is None:
            retained = {"none": True, "original": None, "inverted": None, "random": None}
        else:
            retained = _retention_map(anchor, random_anchor, close_t)
        candidate = Candidate(
            symbol=symbol,
            signal_date=signal_day,
            segment="is" if signal_day < oos_start else "oos",
            day_position=position,
            anchor=anchor,
            random_anchor=random_anchor,
            close_t=close_t,
            retained=retained,
        )
        status, planned, _ = _precheck_candidate(series, market_days, position, facts)
        candidate.precheck = status
        candidate.planned_execution_date = planned
        execution_position = day_pos.get(planned) if planned is not None else None
        if execution_position is not None:
            candidate.trend_bucket = _bucket_execution_trend(series, market_days, execution_position)
            candidate.volatility_bucket = _bucket_execution_volatility(series, market_days, execution_position)
        else:
            candidate.trend_bucket = "unavailable_shape"
            candidate.volatility_bucket = "insufficient_history"
        candidate.gap_bucket = _bucket_gap(series, candidate.planned_execution_date, signal_day)
        candidate.distance_bucket = _bucket_distance(anchor, close_t)
        candidates.append(candidate)

    ledger: list[LedgerEntry] = []
    for candidate in candidates:
        if candidate.precheck != PRECHECK_OK and candidate.precheck.startswith("censored"):
            # facts for planned days of censored candidates are not required; skip bands.
            pass
        series = series_map[candidate.symbol]
        if candidate.precheck == PRECHECK_OK or candidate.precheck == "blocked:buy_limit_up":
            try:
                ledger.extend(
                    _ledger_for_candidate(candidate, series, market_days, candidate.day_position, facts)
                )
            except UnavailableError:
                raise
        else:
            status, reason = candidate.precheck.split(":", 1)
            for arm in ARMS:
                retained = candidate.retained.get(arm)
                if retained is None:
                    ledger.append(
                        LedgerEntry(
                            symbol=candidate.symbol,
                            signal_date=candidate.signal_date,
                            arm=arm,
                            segment=candidate.segment,
                            planned_execution_date=candidate.planned_execution_date,
                            filter_retained=None,
                            precheck=candidate.precheck,
                            terminal_status="censored",
                            terminal_reason="anchor_unavailable",
                            trend_bucket=candidate.trend_bucket,
                            volatility_bucket=candidate.volatility_bucket,
                            gap_bucket=candidate.gap_bucket,
                            distance_bucket=candidate.distance_bucket,
                        )
                    )
                    continue
                ledger.append(
                    LedgerEntry(
                        symbol=candidate.symbol,
                        signal_date=candidate.signal_date,
                        arm=arm,
                        segment=candidate.segment,
                        planned_execution_date=candidate.planned_execution_date,
                        filter_retained=bool(retained),
                        precheck=candidate.precheck,
                        terminal_status=status,
                        terminal_reason=reason,
                        trend_bucket=candidate.trend_bucket,
                        volatility_bucket=candidate.volatility_bucket,
                        gap_bucket=candidate.gap_bucket,
                        distance_bucket=candidate.distance_bucket,
                    )
                )

    none_index = {
        (entry.symbol, entry.signal_date): entry for entry in ledger if entry.arm == "none"
    }
    events: list[dict[str, Any]] = []
    for candidate in candidates:
        per_arm: dict[str, Any] = {}
        for arm in ARMS:
            retained = candidate.retained.get(arm)
            if retained is None:
                per_arm[arm] = {"retained": None, "terminal_status": "censored", "terminal_reason": "anchor_unavailable"}
                continue
            entry = next(
                (
                    item
                    for item in ledger
                    if item.arm == arm and item.symbol == candidate.symbol and item.signal_date == candidate.signal_date
                ),
                None,
            )
            record: dict[str, Any] = {"retained": bool(retained)}
            if entry is not None:
                record.update(
                    {
                        "terminal_status": entry.terminal_status,
                        "terminal_reason": entry.terminal_reason,
                        "exit_reason": entry.exit_reason,
                        "pnl_pct": _round6(entry.pnl_pct),
                    }
                )
                if not retained and arm != "none":
                    donor = none_index.get((candidate.symbol, candidate.signal_date))
                    if donor is None:
                        raise UnavailableError(
                            "engine_ledger_inconsistent",
                            symbol=candidate.symbol,
                            signal_date=candidate.signal_date.isoformat(),
                            field="virtual_outcome_donor_missing",
                        )
                    if donor.terminal_status == "traded":
                        record["virtual_outcome"] = {
                            "source": "none_arm",
                            "pnl_pct": _round6(donor.pnl_pct),
                            "exit_reason": donor.exit_reason,
                            "mae_pct": _round6(donor.mae_pct),
                            "mfe_pct": _round6(donor.mfe_pct),
                        }
                    else:
                        record["virtual_outcome"] = {
                            "source": "none_arm",
                            "status": donor.terminal_status,
                            "reason": donor.terminal_reason,
                        }
            per_arm[arm] = record
        events.append(
            {
                "symbol": candidate.symbol,
                "signal_date": candidate.signal_date.isoformat(),
                "segment": candidate.segment,
                "anchor": _anchor_dict(candidate.anchor),
                "random_anchor": _anchor_dict(candidate.random_anchor),
                "decision": {"price_close_t": _round6(candidate.close_t), "decision_basis": "close_t_vs_anchor"},
                "planned_execution_date": candidate.planned_execution_date.isoformat()
                if candidate.planned_execution_date
                else None,
                "precheck": candidate.precheck,
                "layers": {
                    "trend_bucket": candidate.trend_bucket,
                    "volatility_bucket": candidate.volatility_bucket,
                    "gap_bucket": candidate.gap_bucket,
                    "anchor_distance_bucket": candidate.distance_bucket,
                },
                "arms": per_arm,
            }
        )

    arms_payload: dict[str, Any] = {}
    for arm in ARMS:
        segments_payload: dict[str, Any] = {}
        for segment in ("is", "oos"):
            entries = [entry for entry in ledger if entry.arm == arm and entry.segment == segment]
            segments_payload[segment] = {
                "stats": _segment_stats(
                    sum(1 for candidate in candidates if candidate.segment == segment), entries
                ),
                "layers": _segment_layers(entries),
            }
        arms_payload[arm] = {"segments": segments_payload}

    tnt_contrast = build_tnt_open_anchor_contrast(arms_payload)
    return {
        "factor_id": FACTOR_ID,
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "reasons": [],
        "definition": DEFINITION,
        "provenance": {
            "canonical_generation": canonical.generation(),
            "canonical_manifest_sha256": canonical.manifest_sha256(),
            "markets_generation": markets_generation,
            "markets_manifest_sha256": markets_hash,
            "source_generations_markets": (canonical.manifest().get("source_generations") or {}).get("markets"),
            "oos_start": oos_start.isoformat(),
            "calendar_basis": "pinned_market_days",
            "limit_band_basis": DEFINITION["limit_band_basis"],
            "execution_ledger_version": EXECUTION_LEDGER_VERSION,
            "execution_day_diagnostics": DEFINITION["execution_day_diagnostics"],
            "tnt_contrast_source": TREND_CONTRAST_SOURCE,
            "tnt_contrast_historical_artifacts": [dict(item) for item in TREND_CONTRAST_HISTORICAL_ARTIFACTS],
            "limits": LIMITS,
        },
        "events": events,
        "arms": arms_payload,
        "execution_ledger": [_ledger_dict(entry) for entry in ledger],
        "verdict": _verdict(arms_payload, tnt_contrast),
        "tnt_open_anchor_contrast": tnt_contrast,
        "censored": censored_symbols,
        "disclaimer": DISCLAIMER,
    }


def _anchor_dict(anchor: AnchorInfo | None) -> dict[str, Any] | None:
    if anchor is None:
        return None
    return {
        "date": anchor.day.isoformat(),
        "value": _round6(anchor.value),
        "age_trading_days": anchor.age_trading_days,
        "basis": anchor.basis,
    }


def _ledger_dict(entry: LedgerEntry) -> dict[str, Any]:
    return {
        "symbol": entry.symbol,
        "signal_date": entry.signal_date.isoformat(),
        "arm": entry.arm,
        "segment": entry.segment,
        "planned_execution_date": entry.planned_execution_date.isoformat()
        if entry.planned_execution_date
        else None,
        "filter_retained": entry.filter_retained,
        "precheck": entry.precheck,
        "terminal_status": entry.terminal_status,
        "terminal_reason": entry.terminal_reason,
        "exit_reason": entry.exit_reason,
        "entry_price": _round6(entry.entry_price),
        "exit_price": _round6(entry.exit_price),
        "pnl_pct": _round6(entry.pnl_pct),
        "mae_pct": _round6(entry.mae_pct),
        "mfe_pct": _round6(entry.mfe_pct),
        "blocked_exit_days": entry.blocked_exit_days,
        "engine_entry_index": entry.engine_entry_index,
        "volatility_bucket": entry.volatility_bucket,
    }
