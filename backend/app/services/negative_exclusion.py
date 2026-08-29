"""Pure negative-exclusion (负面排除) detectors and symmetric aggregation (Issue #50).

V1(定义未核实)与 V3(缺少可复核的 PIT 公告源)一律 fail-closed: capability 固定为
unavailable, 模块不提供任何用价格/日线近似公告或分钟路径的入口, 聚合输入携带
V1/V3 信号即报错。

V2/V4/V5 为 available, 冻结口径(前缀封闭, 逐日只用截至当日数据):
- V2: 同日 PIT is_st/风险警示事实任一为真即触发; 事实缺失删失, 不按无信号处理;
- V4: MA5<MA10<MA20 且三线一阶斜率均<0 且 close<MA20, 连续 5 日成立;
- V5: 60 日收盘高点回撤>=30% 且 close 跌破前 20 日(不含当日)平台最低价
  且 当日量>=前 20 日均量 2 倍, 三条件同时成立。

聚合输入为显式 forward returns 的 ObservationRow, 支持逐类开关与全部可用类
组合(all_available); 输出覆盖度、错过反弹/规避下跌的对称统计与组合收益/回撤
增量; 每类独立 verdict(样本不足 unavailable、无稳定改善 rejected); promoted
恒为 False, 不进入 short_pool, 不产生任何交易指令或订单语义。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Final, Mapping, Sequence

from app.services.hold_firm_patterns.models import Bar

DEFINITION_VERSION: Final = "v1"
DEFINITION_DOCUMENT: Final = "docs/ISSUE-50/final-design.md"
RESPONSE_SCHEMA: Final = "negative_exclusion_research/v1"
PROMOTED: Final = False

CLASS_V1: Final = "v1"
CLASS_V2: Final = "v2"
CLASS_V3: Final = "v3"
CLASS_V4: Final = "v4"
CLASS_V5: Final = "v5"
EXCLUSION_CLASSES: Final = (CLASS_V1, CLASS_V2, CLASS_V3, CLASS_V4, CLASS_V5)
EXCLUSION_CLASSES_AVAILABLE: Final = (CLASS_V2, CLASS_V4, CLASS_V5)
COMBINED_LABEL: Final = "all_available"

CAPABILITY_AVAILABLE: Final = "available"
CAPABILITY_V1_UNVERIFIED: Final = "unavailable_definition_unverified"
CAPABILITY_V3_NO_PIT_SOURCE: Final = "unavailable_no_pit_announcement_source"

CLASS_CAPABILITIES: Final[dict[str, str]] = {
    CLASS_V1: CAPABILITY_V1_UNVERIFIED,
    CLASS_V2: CAPABILITY_AVAILABLE,
    CLASS_V3: CAPABILITY_V3_NO_PIT_SOURCE,
    CLASS_V4: CAPABILITY_AVAILABLE,
    CLASS_V5: CAPABILITY_AVAILABLE,
}

VERDICT_ACCEPTED: Final = "accepted"
VERDICT_REJECTED: Final = "rejected"
VERDICT_UNAVAILABLE_CAPABILITY: Final = "unavailable_capability"
VERDICT_INSUFFICIENT_SAMPLES: Final = "unavailable_insufficient_samples"
COMBINED_NO_ENABLED_REASON: Final = "unavailable_no_enabled_available_classes"

CENSOR_PIT_FACT_MISSING: Final = "censor_pit_fact_missing"
CENSOR_WARMUP_INCOMPLETE: Final = "censor_warmup_incomplete"

MA_WINDOWS: Final = (5, 10, 20)
V4_PERSIST_DAYS: Final = 5
V4_WARMUP_DAYS: Final = 20
V5_DRAWDOWN_WINDOW: Final = 60
V5_DRAWDOWN_MIN: Final = 0.30
V5_PLATFORM_WINDOW: Final = 20
V5_VOLUME_MULTIPLE: Final = 2.0
MIN_ACTIVE_SAMPLES: Final = 30


class SignalState(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    CENSORED = "censored"


def capability_for(class_id: str) -> str:
    """显式排除类 capability; 未知类 id 直接报错, 不静默。"""
    capability = CLASS_CAPABILITIES.get(class_id)
    if capability is None:
        raise ValueError(f"unknown exclusion class id: {class_id}")
    return capability


def require_available_class(class_id: str) -> None:
    """fail-closed: unavailable 类与未知类一律拒绝, 不接受代理路径。"""
    capability = capability_for(class_id)
    if capability != CAPABILITY_AVAILABLE:
        raise ValueError(f"{class_id}: {capability} (fail-closed)")


def capability_report() -> dict[str, str]:
    """固定 capability 表快照; V1/V3 永不以代理数据返回 available。"""
    return dict(CLASS_CAPABILITIES)


# ---------------------------------------------------------------------------
# V2: same-day canonical PIT risk-warning/ST fact
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PitNegativeFact:
    """Canonical same-day ``is_st`` fact, covering the local risk-warning/ST flag."""

    is_st: bool


@dataclass(frozen=True, slots=True)
class ClassSignal:
    """单个排除类在单日的确定状态; censored 必须显式携带原因。"""

    state: SignalState
    reason: str | None = None


def detect_v2(fact: PitNegativeFact | None) -> ClassSignal:
    """V2: missing fact is censored; the canonical PIT ST flag activates exclusion."""
    if fact is None:
        return ClassSignal(SignalState.CENSORED, CENSOR_PIT_FACT_MISSING)
    return ClassSignal(SignalState.ACTIVE if fact.is_st else SignalState.INACTIVE)


# ---------------------------------------------------------------------------
# V4: MA5<MA10<MA20, three declining slopes, close<MA20, 5 consecutive days
# ---------------------------------------------------------------------------


def moving_average(values: Sequence[float], end_index: int, window: int) -> float:
    """截至 ``end_index``(含)的 ``window`` 日简单均线; 只用前缀数据。"""
    if end_index + 1 < window:
        raise ValueError("insufficient history for moving average")
    return sum(values[end_index - window + 1 : end_index + 1]) / window


@dataclass(frozen=True, slots=True)
class V4DayConditions:
    aligned: bool
    declining: bool
    below_ma20: bool

    @property
    def all_hold(self) -> bool:
        return self.aligned and self.declining and self.below_ma20


def v4_day_conditions(closes: Sequence[float], index: int) -> V4DayConditions:
    """第 ``index`` 日(0 基)的 V4 三条件; 需要 index>=20 且 index>=1。"""
    if index < 1 or index < V4_WARMUP_DAYS:
        raise ValueError("insufficient history for v4 conditions")
    ma5 = moving_average(closes, index, 5)
    ma10 = moving_average(closes, index, 10)
    ma20 = moving_average(closes, index, 20)
    prev5 = moving_average(closes, index - 1, 5)
    prev10 = moving_average(closes, index - 1, 10)
    prev20 = moving_average(closes, index - 1, 20)
    return V4DayConditions(
        aligned=ma5 < ma10 < ma20,
        declining=ma5 < prev5 and ma10 < prev10 and ma20 < prev20,
        below_ma20=closes[index] < ma20,
    )


def detect_v4_series(closes: Sequence[float]) -> tuple[ClassSignal, ...]:
    """V4 逐日状态: 条件连续 ``V4_PERSIST_DAYS`` 日成立才 active, 前缀封闭。"""
    states: list[ClassSignal] = []
    streak = 0
    for index in range(len(closes)):
        if index < V4_WARMUP_DAYS:
            states.append(ClassSignal(SignalState.CENSORED, CENSOR_WARMUP_INCOMPLETE))
            continue
        if v4_day_conditions(closes, index).all_hold:
            streak += 1
        else:
            streak = 0
        if streak >= V4_PERSIST_DAYS:
            states.append(ClassSignal(SignalState.ACTIVE))
        else:
            states.append(ClassSignal(SignalState.INACTIVE))
    return tuple(states)


# ---------------------------------------------------------------------------
# V5: deep drawdown + platform break + volume surge, all three simultaneously
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class V5Conditions:
    deep_drawdown: bool
    broke_platform: bool
    volume_surge: bool

    @property
    def all_hold(self) -> bool:
        return self.deep_drawdown and self.broke_platform and self.volume_surge


def v5_conditions(bars: Sequence[Bar], index: int) -> V5Conditions:
    """第 ``index`` 日(0 基)的 V5 三条件; 需要 index+1>=60 且 index>=20。"""
    if index + 1 < V5_DRAWDOWN_WINDOW or index < V5_PLATFORM_WINDOW:
        raise ValueError("insufficient history for v5 conditions")
    closes = [bar.research_close_adj for bar in bars]
    rolling_high = max(closes[index - V5_DRAWDOWN_WINDOW + 1 : index + 1])
    drawdown = 1.0 - closes[index] / rolling_high
    platform_bars = bars[index - V5_PLATFORM_WINDOW : index]
    platform_low = min(bar.research_low_adj for bar in platform_bars)
    mean_volume = sum(bar.volume for bar in platform_bars) / V5_PLATFORM_WINDOW
    today = bars[index]
    return V5Conditions(
        deep_drawdown=drawdown >= V5_DRAWDOWN_MIN,
        broke_platform=today.research_close_adj < platform_low,
        volume_surge=today.volume >= V5_VOLUME_MULTIPLE * mean_volume,
    )


def detect_v5_series(bars: Sequence[Bar]) -> tuple[ClassSignal, ...]:
    """V5 逐日状态: 三条件同时成立才 active, 前缀封闭。"""
    states: list[ClassSignal] = []
    for index in range(len(bars)):
        if index + 1 < V5_DRAWDOWN_WINDOW or index < V5_PLATFORM_WINDOW:
            states.append(ClassSignal(SignalState.CENSORED, CENSOR_WARMUP_INCOMPLETE))
            continue
        if v5_conditions(bars, index).all_hold:
            states.append(ClassSignal(SignalState.ACTIVE))
        else:
            states.append(ClassSignal(SignalState.INACTIVE))
    return tuple(states)


# ---------------------------------------------------------------------------
# Symmetric aggregation over explicit forward returns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservationRow:
    """单 symbol-day 观测: 显式 forward return 与各排除类当日状态。"""

    symbol: str
    date: date
    forward_return: float
    signals: Mapping[str, ClassSignal]


@dataclass(frozen=True, slots=True)
class SymmetricStats:
    count: int
    mean_return: float | None
    sum_return: float


@dataclass(frozen=True, slots=True)
class PortfolioDelta:
    full_return: float
    excluded_return: float
    return_delta: float
    full_annualized_return: float
    excluded_annualized_return: float
    annualized_return_delta: float
    full_sharpe: float | None
    excluded_sharpe: float | None
    sharpe_delta: float | None
    full_max_drawdown: float
    excluded_max_drawdown: float
    drawdown_delta: float


@dataclass(frozen=True, slots=True)
class ClassStats:
    key: str
    capability: str
    evaluable_days: int
    censored_days: int
    active_days: int
    coverage: float | None
    missed_rebounds: SymmetricStats
    avoided_declines: SymmetricStats
    net_benefit: float | None
    portfolio: PortfolioDelta | None
    verdict: str
    verdict_reason: str | None


@dataclass(frozen=True, slots=True)
class ExclusionAggregateResult:
    schema: str
    definition_version: str
    enabled_classes: tuple[str, ...]
    classes: Mapping[str, ClassStats]
    combined: ClassStats
    promoted: bool


def _symmetric_stats(values: Sequence[float]) -> SymmetricStats:
    if not values:
        return SymmetricStats(count=0, mean_return=None, sum_return=0.0)
    total = sum(values)
    return SymmetricStats(count=len(values), mean_return=total / len(values), sum_return=total)


def _max_drawdown(day_returns: Sequence[float]) -> float:
    curve = 1.0
    peak = 1.0
    drawdown = 0.0
    for day_return in day_returns:
        curve *= 1.0 + day_return
        peak = max(peak, curve)
        drawdown = max(drawdown, 1.0 - curve / peak)
    return drawdown


def _total_return(day_returns: Sequence[float]) -> float:
    compound = 1.0
    for day_return in day_returns:
        compound *= 1.0 + day_return
    return compound - 1.0


def _annualized_return(total_return: float, periods: int, periods_per_year: float) -> float:
    if periods <= 0:
        return 0.0
    terminal = max(0.0, 1.0 + total_return)
    return terminal ** (periods_per_year / periods) - 1.0


def _sharpe(values: Sequence[float], periods_per_year: float) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    if variance <= 0:
        return None
    return mean / math.sqrt(variance) * math.sqrt(periods_per_year)


def _portfolio_delta(
    rows: Sequence[ObservationRow],
    excluded_flags: Sequence[bool],
    *,
    periods_per_year: float,
) -> PortfolioDelta:
    by_date: dict[date, list[float]] = {}
    kept_by_date: dict[date, list[float]] = {}
    for row, excluded in zip(rows, excluded_flags):
        by_date.setdefault(row.date, []).append(row.forward_return)
        if not excluded:
            kept_by_date.setdefault(row.date, []).append(row.forward_return)
    dates = sorted(by_date)
    full_series = [sum(by_date[day]) / len(by_date[day]) for day in dates]
    excluded_series = [
        (sum(kept_by_date[day]) / len(kept_by_date[day]) if day in kept_by_date else 0.0)
        for day in dates
    ]
    full_return = _total_return(full_series)
    excluded_return = _total_return(excluded_series)
    full_dd = _max_drawdown(full_series)
    excluded_dd = _max_drawdown(excluded_series)
    full_annualized = _annualized_return(full_return, len(full_series), periods_per_year)
    excluded_annualized = _annualized_return(
        excluded_return, len(excluded_series), periods_per_year
    )
    full_sharpe = _sharpe(full_series, periods_per_year)
    excluded_sharpe = _sharpe(excluded_series, periods_per_year)
    return PortfolioDelta(
        full_return=full_return,
        excluded_return=excluded_return,
        return_delta=excluded_return - full_return,
        full_annualized_return=full_annualized,
        excluded_annualized_return=excluded_annualized,
        annualized_return_delta=excluded_annualized - full_annualized,
        full_sharpe=full_sharpe,
        excluded_sharpe=excluded_sharpe,
        sharpe_delta=(
            excluded_sharpe - full_sharpe
            if full_sharpe is not None and excluded_sharpe is not None
            else None
        ),
        full_max_drawdown=full_dd,
        excluded_max_drawdown=excluded_dd,
        drawdown_delta=excluded_dd - full_dd,
    )


def _net_benefit(active_rows: Sequence[ObservationRow]) -> float:
    avoided = sum(-row.forward_return for row in active_rows if row.forward_return < 0)
    missed = sum(row.forward_return for row in active_rows if row.forward_return > 0)
    return avoided - missed


def _verdict_for(active_rows: Sequence[ObservationRow], capability: str) -> tuple[str, str | None]:
    if capability != CAPABILITY_AVAILABLE:
        return VERDICT_UNAVAILABLE_CAPABILITY, capability
    if len(active_rows) < MIN_ACTIVE_SAMPLES:
        return (
            VERDICT_INSUFFICIENT_SAMPLES,
            f"active_days {len(active_rows)} < {MIN_ACTIVE_SAMPLES}",
        )
    ordered = sorted(active_rows, key=lambda row: (row.date, row.symbol))
    midpoint = len(ordered) // 2
    halves = (ordered[:midpoint], ordered[midpoint:])
    overall = _net_benefit(ordered)
    if overall <= 0:
        return VERDICT_REJECTED, "net_benefit_not_positive_overall"
    for label, half in (("first", halves[0]), ("second", halves[1])):
        if not half or _net_benefit(half) <= 0:
            return VERDICT_REJECTED, f"net_benefit_not_positive_in_{label}_half"
    return VERDICT_ACCEPTED, None


def _validate_rows(
    rows: Sequence[ObservationRow], enabled: Sequence[str]
) -> tuple[ObservationRow, ...]:
    if not rows:
        raise ValueError("observation rows must not be empty")
    seen: set[tuple[str, date]] = set()
    ordered: list[ObservationRow] = []
    for row in rows:
        key = (row.symbol, row.date)
        if key in seen:
            raise ValueError(f"duplicate observation row: {key}")
        seen.add(key)
        if not math.isfinite(row.forward_return):
            raise ValueError(f"non-finite forward return for {key}")
        for class_id, signal in row.signals.items():
            capability_for(class_id)
            if signal.state is SignalState.CENSORED and not signal.reason:
                raise ValueError(f"censored signal requires reason for {key}/{class_id}")
            if capability_for(class_id) != CAPABILITY_AVAILABLE:
                raise ValueError(f"{class_id}: signal provided for unavailable class (fail-closed)")
        for class_id in enabled:
            if class_id not in row.signals:
                raise ValueError(f"observation row {key} missing signal for {class_id}")
        ordered.append(row)
    ordered.sort(key=lambda row: (row.date, row.symbol))
    return tuple(ordered)


def _resolve_state(signals: Mapping[str, ClassSignal], contributors: Sequence[str]) -> ClassSignal:
    states = [signals[class_id] for class_id in contributors]
    if any(signal.state is SignalState.ACTIVE for signal in states):
        return ClassSignal(SignalState.ACTIVE)
    censored = [signal for signal in states if signal.state is SignalState.CENSORED]
    if censored:
        reasons = sorted({signal.reason or "unknown" for signal in censored})
        return ClassSignal(SignalState.CENSORED, "+".join(reasons))
    return ClassSignal(SignalState.INACTIVE)


def _class_stats(
    key: str,
    capability: str,
    rows: Sequence[ObservationRow],
    states: Sequence[ClassSignal],
    *,
    periods_per_year: float,
) -> ClassStats:
    evaluable_pairs = [
        (row, signal)
        for row, signal in zip(rows, states)
        if signal.state is not SignalState.CENSORED
    ]
    evaluable = [row for row, _ in evaluable_pairs]
    active_rows = [row for row, signal in evaluable_pairs if signal.state is SignalState.ACTIVE]
    censored = sum(1 for signal in states if signal.state is SignalState.CENSORED)
    coverage = len(active_rows) / len(evaluable) if evaluable else None
    missed = [row.forward_return for row in active_rows if row.forward_return > 0]
    avoided = [row.forward_return for row in active_rows if row.forward_return < 0]
    verdict, verdict_reason = _verdict_for(active_rows, capability)
    portfolio = None
    if evaluable_pairs:
        portfolio = _portfolio_delta(
            evaluable,
            [signal.state is SignalState.ACTIVE for _, signal in evaluable_pairs],
            periods_per_year=periods_per_year,
        )
    return ClassStats(
        key=key,
        capability=capability,
        evaluable_days=len(evaluable),
        censored_days=censored,
        active_days=len(active_rows),
        coverage=coverage,
        missed_rebounds=_symmetric_stats(missed),
        avoided_declines=_symmetric_stats(avoided),
        net_benefit=_net_benefit(active_rows) if active_rows else None,
        portfolio=portfolio,
        verdict=verdict,
        verdict_reason=verdict_reason,
    )


def aggregate_exclusion(
    rows: Sequence[ObservationRow],
    enabled_classes: Sequence[str] | None = None,
    *,
    periods_per_year: float = 252.0,
) -> ExclusionAggregateResult:
    """逐类开关与全部可用类组合的对称聚合; 每类独立 verdict; promoted 恒 False。"""
    enabled = tuple(enabled_classes) if enabled_classes is not None else EXCLUSION_CLASSES_AVAILABLE
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    for class_id in enabled:
        require_available_class(class_id)
    ordered = _validate_rows(rows, enabled)
    contributors = tuple(
        class_id for class_id in enabled if capability_for(class_id) == CAPABILITY_AVAILABLE
    )

    classes: dict[str, ClassStats] = {}
    for class_id in EXCLUSION_CLASSES:
        capability = capability_for(class_id)
        if class_id in enabled:
            states = tuple(row.signals[class_id] for row in ordered)
        else:
            states = tuple(ClassSignal(SignalState.CENSORED, "class_not_enabled") for _ in ordered)
        classes[class_id] = _class_stats(
            class_id,
            capability,
            ordered,
            states,
            periods_per_year=periods_per_year,
        )

    if contributors:
        combined_states = tuple(_resolve_state(row.signals, contributors) for row in ordered)
        combined = _class_stats(
            COMBINED_LABEL,
            CAPABILITY_AVAILABLE,
            ordered,
            combined_states,
            periods_per_year=periods_per_year,
        )
    else:
        combined = ClassStats(
            key=COMBINED_LABEL,
            capability=COMBINED_NO_ENABLED_REASON,
            evaluable_days=0,
            censored_days=len(ordered),
            active_days=0,
            coverage=None,
            missed_rebounds=_symmetric_stats(()),
            avoided_declines=_symmetric_stats(()),
            net_benefit=None,
            portfolio=None,
            verdict=VERDICT_UNAVAILABLE_CAPABILITY,
            verdict_reason=COMBINED_NO_ENABLED_REASON,
        )

    return ExclusionAggregateResult(
        schema=RESPONSE_SCHEMA,
        definition_version=DEFINITION_VERSION,
        enabled_classes=enabled,
        classes=classes,
        combined=combined,
        promoted=PROMOTED,
    )
