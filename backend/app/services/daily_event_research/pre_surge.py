"""Pure, pre-registered pre-surge feature detectors for Issue #47.

F1 近期 PIT 涨停、F2 向上跳空后三个完整交易日未回补、F3 标的连续阳线强于基准、
F4 连续量能堆积，以及四者组合。所有判定只消费显式传入的 ``Bar`` /
``MarketFact`` / 基准 ``Bar`` / 交易日历：无 IO、无时钟、无随机数、无网络。
future surge 标签只允许进入 :class:`PreSurgeStudyAggregator`，绝不进入检测器。

删失语义（显式，绝不静默跳过）：
- 缺 PIT 市场事实 → ``MISSING_MARKET_FACT``
- 无已发布涨停价且制度未知 → ``UNKNOWN_LIMIT_RULE``
- 信号窗口本身尚不存在 / 窗口内缺棒 → ``INSUFFICIENT_WINDOW``
- 基准缺失或基准棒缺失 → ``MISSING_BENCHMARK``
- 指标暖机未完成 → ``WARMUP_INSUFFICIENT``
- F2 确认日尚不在输入中 → 不产出（pending），与删失严格区分。

frozen 契约注意：``models.Detection.censor`` 标注为 ``CensorReason | None``，
本模块按 Issue #47 契约使用本模块枚举 ``PreSurgeCensorReason`` 填充该字段
（str 枚举，dataclass 不做运行期校验）；主线程统一导出时保持原样透传。
"""

from __future__ import annotations
import math

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Final, Mapping, Sequence

from app.data_providers.fquant.daily_market_research import MarketFact
from app.services.hold_firm_patterns.models import Bar

from .models import Detection, DetectionEvidence

DETECTOR_ID: Final = "pre_surge"
VARIANT_F1: Final = "f1_limit_up"
VARIANT_F2: Final = "f2_gap_unfilled"
VARIANT_F3: Final = "f3_relative_bullish"
VARIANT_F4: Final = "f4_volume_stack"
VARIANT_COMBINED: Final = "combined_pre_surge"
ALL_VARIANTS: Final = (VARIANT_F1, VARIANT_F2, VARIANT_F3, VARIANT_F4, VARIANT_COMBINED)
FACTOR_VARIANTS: Final = (VARIANT_F1, VARIANT_F2, VARIANT_F3, VARIANT_F4)
HYPOTHESIS_LABEL: Final = "pre_surge_features_v1"
PRICE_EPSILON_REL: Final = 1e-6

# PIT 制度→涨停幅度的冻结镜像（与 app.data_providers.fquant.daily_market_research.REGIME_PCT
# 保持一致；修改任一侧必须同步）。仅作 published_limit_up（ztj）缺失时的回退。
REGIME_LIMIT_PCT: Final[Mapping[str, float]] = {
    "main_10": 0.10,
    "st_5": 0.05,
    "chinext_20": 0.20,
    "star_20": 0.20,
    "beijing_30": 0.30,
}


class PreSurgeCensorReason(str, Enum):
    MISSING_MARKET_FACT = "censor_missing_market_fact"
    UNKNOWN_LIMIT_RULE = "censor_unknown_limit_rule"
    INSUFFICIENT_WINDOW = "censor_insufficient_window"
    MISSING_BENCHMARK = "censor_missing_benchmark"
    WARMUP_INSUFFICIENT = "censor_warmup_insufficient"


class PreSurgeVerdict(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PreSurgeParams:
    """Frozen, published detector parameters (Issue #47 final design)."""

    f1_lookback_days: int = 10
    min_gap_pct: float = 0.02
    gap_confirm_days: int = 3
    f3_min_streak: int = 2
    f3_window_days: int = 10
    f4_stack_days: int = 5
    f4_baseline_days: int = 20
    combined_min_factors: int = 3
    study_min_samples: int = 30
    study_min_lift: float = 1.15

    def __post_init__(self) -> None:
        if self.f1_lookback_days < 1:
            raise ValueError("f1_lookback_days must be >= 1")
        if not 0.0 < self.min_gap_pct < 1.0:
            raise ValueError("min_gap_pct must be in (0, 1)")
        if self.gap_confirm_days < 1:
            raise ValueError("gap_confirm_days must be >= 1")
        if self.f3_min_streak < 1:
            raise ValueError("f3_min_streak must be >= 1")
        if self.f3_window_days < self.f3_min_streak:
            raise ValueError("f3_window_days must be >= f3_min_streak")
        if self.f4_stack_days < 1 or self.f4_baseline_days < 1:
            raise ValueError("f4_stack_days and f4_baseline_days must be >= 1")
        if not 1 <= self.combined_min_factors <= len(FACTOR_VARIANTS):
            raise ValueError("combined_min_factors must be in [1, 4]")
        if self.study_min_samples < 1:
            raise ValueError("study_min_samples must be >= 1")
        if self.study_min_lift <= 0.0:
            raise ValueError("study_min_lift must be > 0")


DEFAULT_PARAMS: Final = PreSurgeParams()


@dataclass(frozen=True, slots=True)
class _Series:
    cal: tuple[date, ...]
    by_date: Mapping[date, Bar]


@dataclass(frozen=True, slots=True)
class _DayOutcome:
    censor: PreSurgeCensorReason | None
    qualified: bool = False
    values: Mapping[str, object] = field(default_factory=dict)


def _prepare(bars: Sequence[Bar], calendar: Sequence[date]) -> _Series | None:
    cal = tuple(calendar)
    ordered = tuple(bars)
    if not cal or not ordered:
        return None
    if any(cal[index] >= cal[index + 1] for index in range(len(cal) - 1)):
        raise ValueError("calendar must be strictly ascending")
    known = set(cal)
    previous: date | None = None
    for bar in ordered:
        if previous is not None and bar.date <= previous:
            raise ValueError("bars must be strictly ascending by date")
        if bar.date not in known:
            raise ValueError(f"bar date {bar.date} missing from calendar")
        previous = bar.date
    return _Series(cal, {bar.date: bar for bar in ordered})


def _resolve_limit_price(fact: MarketFact) -> tuple[float, str] | None:
    """Resolve the exact PIT limit-up price without current-state inference."""
    if fact.published_limit_up is not None:
        return float(fact.published_limit_up), "published_ztj"
    if fact.pre_close is None:
        return None
    if fact.is_st is True:
        return fact.pre_close * 1.05, "pit_is_st:5pct"
    regime = fact.regime
    if regime is not None:
        pct = REGIME_LIMIT_PCT.get(regime)
        if pct is not None:
            return fact.pre_close * (1.0 + pct), f"regime:{regime}"
    return None


def _f1_day(
    series: _Series,
    facts: Mapping[tuple[str, date], MarketFact],
    index: int,
    params: PreSurgeParams,
) -> _DayOutcome:
    lookback = params.f1_lookback_days
    cal = series.cal
    if index < lookback - 1:
        return _DayOutcome(PreSurgeCensorReason.INSUFFICIENT_WINDOW)
    window = cal[index - lookback + 1 : index + 1]
    hits: list[date] = []
    sources: dict[str, int] = {}
    last_hit_close: float | None = None
    last_hit_limit: float | None = None
    for day in window:
        fact = facts.get((_symbol_of(series), day))
        if fact is None:
            return _DayOutcome(PreSurgeCensorReason.MISSING_MARKET_FACT)
        resolved = _resolve_limit_price(fact)
        if resolved is None:
            return _DayOutcome(PreSurgeCensorReason.UNKNOWN_LIMIT_RULE)
        limit_price, source = resolved
        bar = series.by_date.get(day)
        if bar is None:
            # 停牌/无成交：无收盘价即无涨停，事实仍在，不计命中。
            continue
        if bar.quote_close_raw >= limit_price * (1.0 - PRICE_EPSILON_REL):
            hits.append(day)
            sources[source] = sources.get(source, 0) + 1
            last_hit_close = bar.quote_close_raw
            last_hit_limit = limit_price
    values: dict[str, object] = {
        "hypothesis_label": HYPOTHESIS_LABEL,
        "available_date": cal[index].isoformat(),
        "window_start": window[0].isoformat(),
        "window_end": window[-1].isoformat(),
        "lookback_days": lookback,
        "hit_dates": [day.isoformat() for day in hits],
        "hit_count": len(hits),
        "limit_sources": sources,
        "last_hit_close": last_hit_close,
        "last_hit_limit": last_hit_limit,
    }
    return _DayOutcome(None, bool(hits), values)


def _symbol_of(series: _Series) -> str:
    return next(iter(series.by_date.values())).symbol


_GAP_NOT_GAP: Final = "not_gap"
_GAP_PENDING: Final = "pending"
_GAP_CENSORED: Final = "censored"
_GAP_EVALUATED: Final = "evaluated"


def _evaluate_gap(
    series: _Series, gap_index: int, params: PreSurgeParams
) -> tuple[str, _DayOutcome]:
    """评估以 ``gap_index`` 为缺口日的候选。

    状态：not_gap（非缺口）/ pending（确认日不在输入内，不产出）/
    censored（窗口内缺棒，显式删失）/ evaluated（在 t+confirm 判定）。
    回补检查含缺口日当天：min(low[g .. g+confirm]) > 前收盘 才算未回补。
    """
    cal = series.cal
    if gap_index < 1:
        return _GAP_NOT_GAP, _DayOutcome(None)
    prev_bar = series.by_date.get(cal[gap_index - 1])
    gap_bar = series.by_date.get(cal[gap_index])
    if prev_bar is None or gap_bar is None:
        return _GAP_NOT_GAP, _DayOutcome(None)
    prev_close = prev_bar.quote_close_raw
    gap_open = gap_bar.quote_open_raw
    if gap_open < prev_close * (1.0 + params.min_gap_pct):
        return _GAP_NOT_GAP, _DayOutcome(None)
    confirm_index = gap_index + params.gap_confirm_days
    if confirm_index >= len(cal):
        return _GAP_PENDING, _DayOutcome(None)
    window_bars: list[Bar] = []
    for offset in range(gap_index, confirm_index + 1):
        bar = series.by_date.get(cal[offset])
        if bar is None:
            return _GAP_CENSORED, _DayOutcome(PreSurgeCensorReason.INSUFFICIENT_WINDOW)
        window_bars.append(bar)
    lows = [bar.quote_low_raw for bar in window_bars]
    filled = any(low <= prev_close for low in lows)
    values: dict[str, object] = {
        "hypothesis_label": HYPOTHESIS_LABEL,
        "gap_date": cal[gap_index].isoformat(),
        "available_date": cal[confirm_index].isoformat(),
        "confirm_days": params.gap_confirm_days,
        "min_gap_pct": params.min_gap_pct,
        "gap_open": gap_open,
        "prev_close": prev_close,
        "gap_pct": gap_open / prev_close - 1.0,
        "refill_level": prev_close,
        "min_window_low": min(lows),
        "window_dates": [bar.date.isoformat() for bar in window_bars],
        "filled": filled,
    }
    return _GAP_EVALUATED, _DayOutcome(None, not filled, values)


def _f2_at(series: _Series, index: int, params: PreSurgeParams) -> _DayOutcome:
    """以确认日视角评估 F2：缺口日固定为 ``index - gap_confirm_days``。"""
    cal = series.cal
    if index < params.gap_confirm_days:
        return _DayOutcome(PreSurgeCensorReason.INSUFFICIENT_WINDOW)
    state, outcome = _evaluate_gap(series, index - params.gap_confirm_days, params)
    if state == _GAP_EVALUATED:
        return outcome
    if state == _GAP_CENSORED:
        return _DayOutcome(outcome.censor)
    if state == _GAP_PENDING:
        return _DayOutcome(PreSurgeCensorReason.INSUFFICIENT_WINDOW)
    return _DayOutcome(
        None,
        False,
        {
            "hypothesis_label": HYPOTHESIS_LABEL,
            "gap_candidate": False,
            "available_date": cal[index].isoformat(),
            "confirm_days": params.gap_confirm_days,
        },
    )


def _streak(series: _Series, index: int, max_streak: int) -> int:
    """以 ``index`` 结尾的连续上涨步数；缺棒即保守截断（宁短勿长）。"""
    by_date = series.by_date
    cal = series.cal
    streak = 0
    while streak < max_streak:
        current_index = index - streak
        previous_index = current_index - 1
        if previous_index < 0:
            break
        current = by_date.get(cal[current_index])
        previous = by_date.get(cal[previous_index])
        if current is None or previous is None:
            break
        if not current.research_close_adj > previous.research_close_adj:
            break
        streak += 1
    return streak


def _f3_day(
    series: _Series, benchmark: _Series | None, index: int, params: PreSurgeParams
) -> _DayOutcome:
    if benchmark is None or not benchmark.by_date:
        return _DayOutcome(PreSurgeCensorReason.MISSING_BENCHMARK)
    cal = series.cal
    depth = params.f3_min_streak
    if index < depth:
        return _DayOutcome(PreSurgeCensorReason.INSUFFICIENT_WINDOW)
    for offset in range(index - depth, index + 1):
        if series.by_date.get(cal[offset]) is None:
            return _DayOutcome(PreSurgeCensorReason.INSUFFICIENT_WINDOW)
        if benchmark.by_date.get(cal[offset]) is None:
            return _DayOutcome(PreSurgeCensorReason.MISSING_BENCHMARK)
    target_streak = _streak(series, index, params.f3_window_days)
    benchmark_streak = _streak(benchmark, index, params.f3_window_days)
    qualified = target_streak >= depth and target_streak > benchmark_streak
    values: dict[str, object] = {
        "hypothesis_label": HYPOTHESIS_LABEL,
        "available_date": cal[index].isoformat(),
        "target_streak": target_streak,
        "benchmark_streak": benchmark_streak,
        "min_streak": depth,
        "window_days": params.f3_window_days,
        "benchmark_symbol": _symbol_of(benchmark),
    }
    return _DayOutcome(None, qualified, values)


def _f4_day(series: _Series, index: int, params: PreSurgeParams) -> _DayOutcome:
    stack = params.f4_stack_days
    baseline = params.f4_baseline_days
    start = index - stack - baseline + 1
    if start < 0:
        return _DayOutcome(PreSurgeCensorReason.WARMUP_INSUFFICIENT)
    cal = series.cal
    volumes: list[float] = []
    for offset in range(start, index + 1):
        bar = series.by_date.get(cal[offset])
        if bar is None:
            return _DayOutcome(PreSurgeCensorReason.WARMUP_INSUFFICIENT)
        volumes.append(bar.volume)
    qualified = True
    ratios: list[float] = []
    failed = False
    for offset in range(index - stack + 1, index + 1):
        position = offset - start
        window = volumes[position - baseline : position]
        mean = sum(window) / baseline
        if mean <= 0.0:
            failed = True
            continue
        ratio = volumes[position] / mean
        ratios.append(ratio)
        if not volumes[position] > mean:
            failed = True
    values: dict[str, object] = {
        "hypothesis_label": HYPOTHESIS_LABEL,
        "available_date": cal[index].isoformat(),
        "stack_days": stack,
        "baseline_days": baseline,
        "min_ratio": min(ratios) if ratios else None,
        "stack_volumes": volumes[-stack:],
    }
    return _DayOutcome(None, qualified and not failed, values)


def _combined_day(
    outcomes: Mapping[str, _DayOutcome], index: int, cal: tuple[date, ...], params: PreSurgeParams
) -> _DayOutcome:
    for variant in FACTOR_VARIANTS:
        outcome = outcomes[variant]
        if outcome.censor is not None:
            return _DayOutcome(outcome.censor)
    flags = {variant: outcomes[variant].qualified for variant in FACTOR_VARIANTS}
    count = sum(1 for flag in flags.values() if flag)
    values: dict[str, object] = {
        "hypothesis_label": HYPOTHESIS_LABEL,
        "available_date": cal[index].isoformat(),
        "factors": flags,
        "qualified_factor_count": count,
        "min_factors": params.combined_min_factors,
        "factor_values": {variant: dict(outcomes[variant].values) for variant in FACTOR_VARIANTS},
    }
    return _DayOutcome(None, count >= params.combined_min_factors, values)


def _scan(series: _Series, symbol: str, variant: str, evaluate) -> tuple[Detection, ...]:
    detections: list[Detection] = []
    for index in range(len(series.cal)):
        if series.by_date.get(series.cal[index]) is None:
            continue
        outcome = evaluate(index)
        if outcome.censor is not None:
            detections.append(
                Detection(DETECTOR_ID, variant, symbol, series.cal[index], censor=outcome.censor)
            )
        else:
            detections.append(
                Detection(
                    DETECTOR_ID,
                    variant,
                    symbol,
                    series.cal[index],
                    evidence=DetectionEvidence(outcome.qualified, outcome.values),
                )
            )
    return tuple(detections)


def _facts_view(
    facts: Mapping[tuple[str, date], MarketFact] | None,
) -> Mapping[tuple[str, date], MarketFact]:
    return {} if facts is None else facts


def detect_f1_limit_up(
    symbol: str,
    bars: Sequence[Bar],
    facts: Mapping[tuple[str, date], MarketFact] | None,
    calendar: Sequence[date],
    params: PreSurgeParams = DEFAULT_PARAMS,
) -> tuple[Detection, ...]:
    """F1：近 ``f1_lookback_days`` 个交易日内存在 PIT 涨停日。"""
    series = _prepare(bars, calendar)
    if series is None:
        return ()
    facts_map = _facts_view(facts)
    return _scan(
        series, symbol, VARIANT_F1, lambda index: _f1_day(series, facts_map, index, params)
    )


def _detect_f2(series: _Series, symbol: str, params: PreSurgeParams) -> tuple[Detection, ...]:
    detections: list[Detection] = []
    for gap_index in range(1, len(series.cal)):
        state, outcome = _evaluate_gap(series, gap_index, params)
        if state not in (_GAP_EVALUATED, _GAP_CENSORED):
            continue
        signal_day = series.cal[gap_index + params.gap_confirm_days]
        if state == _GAP_CENSORED:
            detections.append(
                Detection(DETECTOR_ID, VARIANT_F2, symbol, signal_day, censor=outcome.censor)
            )
        else:
            detections.append(
                Detection(
                    DETECTOR_ID,
                    VARIANT_F2,
                    symbol,
                    signal_day,
                    evidence=DetectionEvidence(outcome.qualified, outcome.values),
                )
            )
    return tuple(detections)


def detect_f2_gap_unfilled(
    symbol: str,
    bars: Sequence[Bar],
    calendar: Sequence[date],
    params: PreSurgeParams = DEFAULT_PARAMS,
) -> tuple[Detection, ...]:
    """F2：向上跳空后 ``gap_confirm_days`` 个完整交易日未回补。

    Detection 只在确认日（缺口日 + 3 个完整交易日）产出，
    ``signal_date`` 即确认日，绝不回填缺口日；确认日未入输入则 pending 不产出。
    """
    series = _prepare(bars, calendar)
    if series is None:
        return ()
    return _scan(
        series,
        symbol,
        VARIANT_F2,
        lambda index: _f2_at(series, index, params),
    )


def detect_f3_relative_bullish(
    symbol: str,
    bars: Sequence[Bar],
    benchmark_bars: Sequence[Bar] | None,
    calendar: Sequence[date],
    params: PreSurgeParams = DEFAULT_PARAMS,
) -> tuple[Detection, ...]:
    """F3：标的连续阳线（连续上涨收盘）强于基准连续阳线。"""
    series = _prepare(bars, calendar)
    if series is None:
        return ()
    benchmark = _prepare(benchmark_bars, calendar) if benchmark_bars else None
    return _scan(
        series, symbol, VARIANT_F3, lambda index: _f3_day(series, benchmark, index, params)
    )


def detect_f4_volume_stack(
    symbol: str,
    bars: Sequence[Bar],
    calendar: Sequence[date],
    params: PreSurgeParams = DEFAULT_PARAMS,
) -> tuple[Detection, ...]:
    """F4：最近 ``f4_stack_days`` 日每日成交量均高于前 ``f4_baseline_days`` 日均值。"""
    series = _prepare(bars, calendar)
    if series is None:
        return ()
    return _scan(series, symbol, VARIANT_F4, lambda index: _f4_day(series, index, params))


def detect_combined(
    symbol: str,
    bars: Sequence[Bar],
    facts: Mapping[tuple[str, date], MarketFact] | None,
    benchmark_bars: Sequence[Bar] | None,
    calendar: Sequence[date],
    params: PreSurgeParams = DEFAULT_PARAMS,
) -> tuple[Detection, ...]:
    """组合：四因子当日均可评估时，命中因子数 ≥ ``combined_min_factors``。"""
    series = _prepare(bars, calendar)
    if series is None:
        return ()
    facts_map = _facts_view(facts)
    benchmark = _prepare(benchmark_bars, calendar) if benchmark_bars else None

    def evaluate(index: int) -> _DayOutcome:
        outcomes = {
            VARIANT_F1: _f1_day(series, facts_map, index, params),
            VARIANT_F2: _f2_at(series, index, params),
            VARIANT_F3: _f3_day(series, benchmark, index, params),
            VARIANT_F4: _f4_day(series, index, params),
        }
        return _combined_day(outcomes, index, series.cal, params)

    return _scan(series, symbol, VARIANT_COMBINED, evaluate)


class PreSurgeDetector:
    """一次产出全部单因子与组合 Detection 的门面（每因子可独立调用/序列化）。"""

    def __init__(self, params: PreSurgeParams = DEFAULT_PARAMS) -> None:
        self.params = params

    @property
    def detector_id(self) -> str:
        return DETECTOR_ID

    @property
    def variant(self) -> str:
        return VARIANT_COMBINED

    def detect(
        self,
        symbol: str,
        bars: Sequence[Bar],
        facts: Mapping[tuple[str, date], MarketFact] | None,
        benchmark_bars: Sequence[Bar] | None,
        calendar: Sequence[date],
    ) -> dict[str, tuple[Detection, ...]]:
        return {
            VARIANT_F1: detect_f1_limit_up(symbol, bars, facts, calendar, self.params),
            VARIANT_F2: detect_f2_gap_unfilled(symbol, bars, calendar, self.params),
            VARIANT_F3: detect_f3_relative_bullish(
                symbol, bars, benchmark_bars, calendar, self.params
            ),
            VARIANT_F4: detect_f4_volume_stack(symbol, bars, calendar, self.params),
            VARIANT_COMBINED: detect_combined(
                symbol, bars, facts, benchmark_bars, calendar, self.params
            ),
        }


@dataclass(frozen=True, slots=True)
class PreSurgeFactorStats:
    factor: str
    evaluated: int
    qualified: int
    surge_total: int
    qualified_and_surge: int
    censored: int
    necessary_rate: float | None
    sufficient_rate: float | None
    baseline_rate: float | None
    lift: float | None
    sufficient_increment_ci95_lower: float | None
    verdict: PreSurgeVerdict


@dataclass(slots=True)
class _Counts:
    evaluated: int = 0
    qualified: int = 0
    surge_total: int = 0
    qualified_and_surge: int = 0
    censored: int = 0


class PreSurgeStudyAggregator:
    """Issue #47/#48 窄研究聚合：必要/充分方向统计与逐因子独立 verdict。

    - 必要方向 ``necessary_rate = P(feature | future surge)``：纯描述性统计，
      绝不作为预测价值或 verdict 依据。
    - 充分方向 ``sufficient_rate = P(future surge | feature)``，与无条件基线
      ``baseline_rate = P(future surge)``（同分布随机对照）相除得 ``lift``。
    - verdict 只由充分方向决定：qualified 样本 < ``study_min_samples`` 或基线
      不可定义 → unavailable；lift < ``study_min_lift`` → rejected；否则 supported。
    - future surge 标签只在本聚合器内使用；``None`` 标签（前瞻不完整）不进
      任何分母；删失 Detection 只计数不进分母。
    - 每个因子（含组合 ``combined_pre_surge``）verdict 完全独立。
    """

    def __init__(self, params: PreSurgeParams = DEFAULT_PARAMS) -> None:
        self.params = params
        self._counts: dict[str, _Counts] = {}

    def record(self, detection: Detection, future_surge: bool | None) -> None:
        counts = self._counts.setdefault(detection.variant, _Counts())
        if detection.censor is not None:
            counts.censored += 1
            return
        if future_surge is None:
            return
        counts.evaluated += 1
        qualified = detection.evidence is not None and detection.evidence.qualified
        if future_surge:
            counts.surge_total += 1
            if qualified:
                counts.qualified_and_surge += 1
        if qualified:
            counts.qualified += 1

    def summarize(self) -> dict[str, PreSurgeFactorStats]:
        stats: dict[str, PreSurgeFactorStats] = {}
        for factor, counts in self._counts.items():
            necessary = (
                counts.qualified_and_surge / counts.surge_total if counts.surge_total else None
            )
            sufficient = counts.qualified_and_surge / counts.qualified if counts.qualified else None
            baseline = counts.surge_total / counts.evaluated if counts.evaluated else None
            lift = (
                sufficient / baseline
                if sufficient is not None and baseline is not None and baseline > 0.0
                else None
            )
            increment = (
                sufficient - baseline if sufficient is not None and baseline is not None else None
            )
            standard_error = (
                math.sqrt(
                    sufficient * (1.0 - sufficient) / counts.qualified
                    + baseline * (1.0 - baseline) / counts.evaluated
                )
                if sufficient is not None
                and baseline is not None
                and counts.qualified > 0
                and counts.evaluated > 0
                else None
            )
            increment_ci95_lower = (
                increment - 1.96 * standard_error
                if increment is not None and standard_error is not None
                else None
            )
            if (
                counts.qualified < self.params.study_min_samples
                or sufficient is None
                or baseline is None
                or baseline <= 0.0
                or increment_ci95_lower is None
            ):
                verdict = PreSurgeVerdict.UNAVAILABLE
            elif lift is None or lift < self.params.study_min_lift or increment_ci95_lower <= 0.0:
                verdict = PreSurgeVerdict.REJECTED
            else:
                verdict = PreSurgeVerdict.ACCEPTED
            stats[factor] = PreSurgeFactorStats(
                factor=factor,
                evaluated=counts.evaluated,
                qualified=counts.qualified,
                surge_total=counts.surge_total,
                qualified_and_surge=counts.qualified_and_surge,
                censored=counts.censored,
                necessary_rate=necessary,
                sufficient_rate=sufficient,
                baseline_rate=baseline,
                lift=lift,
                sufficient_increment_ci95_lower=increment_ci95_lower,
                verdict=verdict,
            )
        return stats


ANNUALIZATION_TRADING_DAYS: Final = 252

# 风险/可达指标口径（随评测输出透出，禁止隐含假设）。
RISK_METRIC_DEFINITIONS: Final[Mapping[str, str]] = {
    "price_basis": "canonical_research_adjusted_close_open",
    "entry": "next_trading_day_open_adj",
    "exit": "horizon_last_day_close_adj",
    "cost": "round_trip_net_after_cost_bps",
    "portfolio": "equal_weight_by_entry_date_cumulative_nav",
    "annualization": "sqrt(252)_per_event_day_observation",
    "sortino_downside": "full_sample_root_mean_square_of_negative_day_returns",
    "turnover": "mean_daily_entries_plus_exits_over_twice_open_positions",
    "unreachable": "entry_pit_limit_up_or_exit_pit_limit_down_blocked",
    "sample": "qualified_events_with_complete_horizon_only",
}


@dataclass(frozen=True, slots=True)
class PreSurgeArmEventReturn:
    """单个 qualified 事件的执行口径收益（风险指标账本输入）。"""

    entry_date: date
    exit_date: date
    net_return: float | None
    reachable: bool


@dataclass(frozen=True, slots=True)
class PreSurgeArmRiskMetrics:
    """per-arm（per-variant）风险/可达指标。"""

    events: int
    unreachable_events: int
    achievable_events: int
    achievable_mean_return: float | None
    max_drawdown: float | None
    sharpe: float | None
    sortino: float | None
    turnover: float | None


def _arm_risk_metrics(events: Sequence[PreSurgeArmEventReturn]) -> PreSurgeArmRiskMetrics:
    """由事件收益序列计算风险指标；不可达事件只计数、不进入收益序列。"""
    achievable = [
        item for item in events if item.reachable and item.net_return is not None
    ]
    unreachable = len(events) - len(achievable)
    if not achievable:
        return PreSurgeArmRiskMetrics(
            events=len(events),
            unreachable_events=unreachable,
            achievable_events=0,
            achievable_mean_return=None,
            max_drawdown=None,
            sharpe=None,
            sortino=None,
            turnover=None,
        )

    returns = [item.net_return for item in achievable if item.net_return is not None]
    by_day: dict[date, list[float]] = {}
    for item in achievable:
        if item.net_return is not None:
            by_day.setdefault(item.entry_date, []).append(item.net_return)
    day_returns = [sum(values) / len(values) for _, values in sorted(by_day.items())]

    peak = nav = 1.0
    max_drawdown = 0.0
    for day_return in day_returns:
        nav *= 1.0 + day_return
        peak = max(peak, nav)
        max_drawdown = max(max_drawdown, (peak - nav) / peak)

    mean_day = sum(day_returns) / len(day_returns)
    sharpe = None
    if len(day_returns) >= 2:
        variance = sum((value - mean_day) ** 2 for value in day_returns) / (len(day_returns) - 1)
        std = math.sqrt(variance)
        if std > 0.0:
            sharpe = mean_day / std * math.sqrt(ANNUALIZATION_TRADING_DAYS)
    downside = math.sqrt(sum(min(value, 0.0) ** 2 for value in day_returns) / len(day_returns))
    sortino = (
        mean_day / downside * math.sqrt(ANNUALIZATION_TRADING_DAYS)
        if downside > 0.0
        else None
    )

    grid = sorted({item.entry_date for item in achievable} | {item.exit_date for item in achievable})
    daily_turnover: list[float] = []
    for day in grid:
        open_positions = sum(item.entry_date <= day <= item.exit_date for item in achievable)
        if open_positions == 0:
            continue
        entries = sum(item.entry_date == day for item in achievable)
        exits = sum(item.exit_date == day for item in achievable)
        daily_turnover.append((entries + exits) / (2 * open_positions))
    turnover = sum(daily_turnover) / len(daily_turnover) if daily_turnover else None

    return PreSurgeArmRiskMetrics(
        events=len(events),
        unreachable_events=unreachable,
        achievable_events=len(achievable),
        achievable_mean_return=sum(returns) / len(returns),
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        sortino=sortino,
        turnover=turnover,
    )


class PreSurgeArmRiskLedger:
    """per-arm（per-variant）风险/可达指标账本，与 verdict 完全解耦。"""

    def __init__(self) -> None:
        self._events: dict[str, list[PreSurgeArmEventReturn]] = {}

    def record(self, variant: str, event: PreSurgeArmEventReturn) -> None:
        self._events.setdefault(variant, []).append(event)

    def metrics(self) -> dict[str, PreSurgeArmRiskMetrics]:
        return {variant: _arm_risk_metrics(events) for variant, events in self._events.items()}


def _jsonify(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


def detection_payload(detection: Detection) -> dict[str, object]:
    """JSON 安全的 Detection 序列化（日期 → ISO，枚举 → 值，嵌套递归）。"""
    payload: dict[str, object] = {
        "detector_id": detection.detector_id,
        "variant": detection.variant,
        "symbol": detection.symbol,
        "signal_date": detection.signal_date.isoformat(),
        "evidence": None,
        "censor": detection.censor.value if detection.censor is not None else None,
    }
    if detection.evidence is not None:
        payload["evidence"] = {
            "qualified": detection.evidence.qualified,
            "values": {key: _jsonify(value) for key, value in detection.evidence.values.items()},
        }
    return payload


__all__ = [
    "ALL_VARIANTS",
    "ANNUALIZATION_TRADING_DAYS",
    "DEFAULT_PARAMS",
    "DETECTOR_ID",
    "FACTOR_VARIANTS",
    "HYPOTHESIS_LABEL",
    "PreSurgeArmEventReturn",
    "PreSurgeArmRiskLedger",
    "PreSurgeArmRiskMetrics",
    "RISK_METRIC_DEFINITIONS",
    "PreSurgeCensorReason",
    "PreSurgeDetector",
    "PreSurgeFactorStats",
    "PreSurgeParams",
    "PreSurgeStudyAggregator",
    "PreSurgeVerdict",
    "VARIANT_COMBINED",
    "VARIANT_F1",
    "VARIANT_F2",
    "VARIANT_F3",
    "VARIANT_F4",
    "detect_combined",
    "detect_f1_limit_up",
    "detect_f2_gap_unfilled",
    "detect_f3_relative_bullish",
    "detect_f4_volume_stack",
    "detection_payload",
]
