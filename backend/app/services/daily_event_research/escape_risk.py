"""Pure daily-bar escape-risk detectors and per-signal research (Issue #48).

S1/S8/S9 只读显式传入的内存日线 ``Bar`` 序列, 逐日只用截至当日的收盘前缀,
无网络、无文件写入、无未来函数。分钟信号 S2-S7/S10 缺少不可变分钟历史,
统一声明 ``unavailable_insufficient_immutable_history``, 模块不提供任何
用日线 high/low 近似分钟路径的入口(fail-closed)。

研究聚合对每个信号独立出具 verdict; 卖飞率与规避深度按对称口径呈现,
多信号只按同日触发计数分组, 不产生任何合并方向指令或订单/执行语义。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Final, Mapping, Sequence

from app.services.hold_firm_patterns.models import Bar

from .models import CensorReason, Detection, DetectionEvidence

DETECTOR_ID_S1: Final = "escape_s1"
DETECTOR_ID_S8: Final = "escape_s8"
DETECTOR_ID_S9: Final = "escape_s9"
SIGNAL_VARIANT: Final = "daily_v1"
HYPOTHESIS_LABEL: Final = "issue48_escape_risk_daily_v1"

SIGNAL_ID_BY_DETECTOR: Final[dict[str, str]] = {
    DETECTOR_ID_S1: "s1",
    DETECTOR_ID_S8: "s8",
    DETECTOR_ID_S9: "s9",
}
DETECTOR_ID_BY_SIGNAL: Final[dict[str, str]] = {
    signal_id: detector_id for detector_id, signal_id in SIGNAL_ID_BY_DETECTOR.items()
}

DAILY_SIGNAL_IDS: Final[tuple[str, ...]] = ("s1", "s8", "s9")
MINUTE_SIGNAL_IDS: Final[tuple[str, ...]] = ("s2", "s3", "s4", "s5", "s6", "s7", "s10")

SIGNAL_CAPABILITY_AVAILABLE: Final = "available"
SIGNAL_CAPABILITY_MINUTE_UNAVAILABLE: Final = "unavailable_insufficient_immutable_history"

SIGNAL_CAPABILITIES: Final[dict[str, str]] = {
    **{signal_id: SIGNAL_CAPABILITY_AVAILABLE for signal_id in DAILY_SIGNAL_IDS},
    **{signal_id: SIGNAL_CAPABILITY_MINUTE_UNAVAILABLE for signal_id in MINUTE_SIGNAL_IDS},
}


class EscapeCensorReason(str, Enum):
    """Issue #48 模块级 censor code; 窗口不足复用 models.CensorReason。"""

    BENCHMARK_MISSING = "censor_benchmark_missing"
    PIT_FACT_MISSING = "censor_pit_fact_missing"


NEW_HIGH_WINDOW_DAYS: Final = 60
MACD_FAST_DAYS: Final = 12
MACD_SLOW_DAYS: Final = 26
MACD_SIGNAL_DAYS: Final = 9
MACD_HIST_SCALE: Final = 2.0
# 红柱 hist 从第 MACD_SIGNAL_DAYS 个 DIF 起才脱离 DEA 种子畸变, 之前的 hist 不参与口径。
MACD_MIN_VALID_INDEX: Final = MACD_SLOW_DAYS + MACD_SIGNAL_DAYS - 2
THREE_YIN_DAYS: Final = 3
LOW_OPEN_MIN_PCT: Final = 0.05
ROUND_TRIP_LEGS: Final = 2
COST_BPS_DEFAULT: Final = 10.0
COST_BPS_MAX: Final = 1000.0
DEFAULT_HORIZONS: Final[tuple[int, ...]] = (1, 3, 5, 10)
BASELINE_KINDS: Final[tuple[str, ...]] = ("buy_hold", "ma20", "atr", "prev_close")

BASELINE_STATUS_OK: Final = "ok"
BASELINE_STATUS_UNAVAILABLE: Final = "unavailable_no_baseline"
BENCHMARK_STATUS_OK: Final = "ok"
BENCHMARK_STATUS_UNAVAILABLE: Final = "unavailable_no_benchmark"
VERDICT_BASELINE_UNAVAILABLE: Final = "unavailable_no_frozen_oos_baseline"
VERDICT_NO_EVENTS: Final = "unavailable_no_qualified_events"
VERDICT_BENCHMARK_MISSING: Final = "unavailable_benchmark_missing"


def capability_for(signal_id: str) -> str:
    """显式信号 capability; 未知信号 id 直接报错, 不静默。"""
    capability = SIGNAL_CAPABILITIES.get(signal_id)
    if capability is None:
        raise ValueError(f"unknown escape signal id: {signal_id}")
    return capability


def require_daily_signal(signal_id: str) -> None:
    """fail-closed: 分钟信号与未知信号一律拒绝, 不接受日线近似路径。"""
    capability = capability_for(signal_id)
    if capability != SIGNAL_CAPABILITY_AVAILABLE:
        raise ValueError(
            f"{signal_id}: {capability} "
            "(daily high/low approximation of minute signals is not accepted)"
        )


def _validated_bars(bars: Sequence[Bar], calendar: Sequence[date]) -> tuple[Bar, ...]:
    ordered = tuple(bars)
    cal = tuple(calendar)
    if not ordered or not cal:
        return ordered
    if any(cal[index] >= cal[index + 1] for index in range(len(cal) - 1)):
        raise ValueError("calendar must be strictly ascending")
    cal_dates = set(cal)
    previous: date | None = None
    for bar in ordered:
        if previous is not None and bar.date <= previous:
            raise ValueError("bars must be strictly ascending by date")
        if bar.date not in cal_dates:
            raise ValueError(f"bar date {bar.date} missing from calendar")
        previous = bar.date
    return ordered


def macd_histogram(closes: Sequence[float]) -> list[float]:
    """标准 MACD 红柱 ``2*(DIF-DEA)``。

    口径唯一: EMA 以首收盘为种子递推, DEA 以首个 DIF 为种子递推; 每个点位
    只依赖截至当日的收盘前缀, 满足截断不变性。
    """
    if not closes:
        return []
    alpha_fast = 2.0 / (MACD_FAST_DAYS + 1)
    alpha_slow = 2.0 / (MACD_SLOW_DAYS + 1)
    alpha_signal = 2.0 / (MACD_SIGNAL_DAYS + 1)
    ema_fast = closes[0]
    ema_slow = closes[0]
    dif_values: list[float] = []
    for close in closes:
        ema_fast += alpha_fast * (close - ema_fast)
        ema_slow += alpha_slow * (close - ema_slow)
        dif_values.append(ema_fast - ema_slow)
    dea = dif_values[0]
    histogram: list[float] = []
    for dif in dif_values:
        dea += alpha_signal * (dif - dea)
        histogram.append(MACD_HIST_SCALE * (dif - dea))
    return histogram


@dataclass(frozen=True, slots=True)
class _RedRun:
    start: int  # inclusive
    end: int  # inclusive
    peak: float


def _red_runs(histogram: Sequence[float], first_valid: int) -> list[_RedRun]:
    """hist>0 的连续红柱段; 只统计 first_valid 起的段, 峰值 = 段内最大 hist。"""
    runs: list[_RedRun] = []
    start: int | None = None
    for index in range(first_valid, len(histogram)):
        if histogram[index] > 0:
            if start is None:
                start = index
        elif start is not None:
            runs.append(_RedRun(start, index - 1, max(histogram[start:index])))
            start = None
    if start is not None:
        runs.append(_RedRun(start, len(histogram) - 1, max(histogram[start:])))
    return runs


class EscapeS1Detector:
    """S1: 收盘价创窗口新高 且 MACD 红柱相邻峰值递降(顶背离), 只用日线前缀。

    口径: 新高 = 当日 high 严格大于前 ``NEW_HIGH_WINDOW_DAYS`` 根 high 的最大值;
    当前红柱段峰值 = 段起点至 signal 日(含)的 running max(不看未来);
    相邻段 = 当前段之前最近的一个完整红柱段; 递降 = 当前峰值 < 相邻段峰值。
    """

    @property
    def detector_id(self) -> str:
        return DETECTOR_ID_S1

    @property
    def variant(self) -> str:
        return SIGNAL_VARIANT

    def detect(
        self, symbol: str, bars: Sequence[Bar], calendar: Sequence[date]
    ) -> tuple[Detection, ...]:
        ordered = _validated_bars(bars, calendar)
        if not ordered:
            return ()
        closes = [bar.research_close_adj for bar in ordered]
        highs = [bar.research_high_adj for bar in ordered]
        histogram = macd_histogram(closes)
        runs = _red_runs(histogram, MACD_MIN_VALID_INDEX)
        detections: list[Detection] = []
        for index, bar in enumerate(ordered):
            if index < NEW_HIGH_WINDOW_DAYS:
                # 窗口不足但价格正在创全程新高: 显式 censor, 不静默跳过。
                if index >= 1 and highs[index] > max(highs[:index]):
                    detections.append(
                        Detection(
                            self.detector_id,
                            self.variant,
                            symbol,
                            bar.date,
                            censor=CensorReason.WARMUP_INCOMPLETE,
                        )
                    )
                continue
            prior_high = max(highs[index - NEW_HIGH_WINDOW_DAYS : index])
            if not highs[index] > prior_high:
                continue
            values: dict[str, object] = {
                "available_date": bar.date.isoformat(),
                "available_at_session": "close",
                "existing_position_required": False,
                "new_high": True,
                "new_high_window_days": NEW_HIGH_WINDOW_DAYS,
                "window_high_prior": prior_high,
                "high": highs[index],
                "macd_fast_days": MACD_FAST_DAYS,
                "macd_slow_days": MACD_SLOW_DAYS,
                "macd_signal_days": MACD_SIGNAL_DAYS,
                "macd_hist_scale": MACD_HIST_SCALE,
                "macd_min_valid_index": MACD_MIN_VALID_INDEX,
                "hypothesis_label": HYPOTHESIS_LABEL,
            }
            run = next((item for item in runs if item.start <= index <= item.end), None)
            if run is None or histogram[index] <= 0:
                values["in_red_run"] = False
                detections.append(
                    Detection(
                        self.detector_id,
                        self.variant,
                        symbol,
                        bar.date,
                        evidence=DetectionEvidence(False, values),
                    )
                )
                continue
            previous_run = next((item for item in reversed(runs) if item.end < run.start), None)
            values["in_red_run"] = True
            values["hist"] = histogram[index]
            if previous_run is None:
                values["has_previous_red_run"] = False
                detections.append(
                    Detection(
                        self.detector_id,
                        self.variant,
                        symbol,
                        bar.date,
                        evidence=DetectionEvidence(False, values),
                    )
                )
                continue
            current_peak = max(histogram[run.start : index + 1])
            values["has_previous_red_run"] = True
            values["red_run_current_peak"] = current_peak
            values["red_run_previous_peak"] = previous_run.peak
            values["previous_red_run_end_offset"] = index - previous_run.end
            detections.append(
                Detection(
                    self.detector_id,
                    self.variant,
                    symbol,
                    bar.date,
                    evidence=DetectionEvidence(current_peak < previous_run.peak, values),
                )
            )
        return tuple(detections)


class EscapeS8Detector:
    """S8: 连续 ``THREE_YIN_DAYS`` 根 ``close < open`` 的三连阴(实心阴线, 十字剔除)。"""

    @property
    def detector_id(self) -> str:
        return DETECTOR_ID_S8

    @property
    def variant(self) -> str:
        return SIGNAL_VARIANT

    def detect(
        self, symbol: str, bars: Sequence[Bar], calendar: Sequence[date]
    ) -> tuple[Detection, ...]:
        ordered = _validated_bars(bars, calendar)
        if not ordered:
            return ()
        yin = [bar.research_close_adj < bar.research_open_adj for bar in ordered]
        detections: list[Detection] = []
        for index, bar in enumerate(ordered):
            if index < THREE_YIN_DAYS - 1:
                if yin[index]:
                    detections.append(
                        Detection(
                            self.detector_id,
                            self.variant,
                            symbol,
                            bar.date,
                            censor=CensorReason.WARMUP_INCOMPLETE,
                        )
                    )
                continue
            flags = (yin[index - 2], yin[index - 1], yin[index])
            values: dict[str, object] = {
                "available_date": bar.date.isoformat(),
                "available_at_session": "close",
                "existing_position_required": False,
                "yin_prev2": flags[0],
                "yin_prev1": flags[1],
                "yin_today": flags[2],
                "three_yin_days": THREE_YIN_DAYS,
                "open": bar.research_open_adj,
                "close": bar.research_close_adj,
                "hypothesis_label": HYPOTHESIS_LABEL,
            }
            detections.append(
                Detection(
                    self.detector_id,
                    self.variant,
                    symbol,
                    bar.date,
                    evidence=DetectionEvidence(all(flags), values),
                )
            )
        return tuple(detections)


class EscapeS9Detector:
    """S9: 当日开盘相对昨收低开 >= 5%; available_date 为当日(开盘时点可知)。

    昨收 = 上一根日线原始收盘，开盘 = 当日原始开盘；序列首根或原始价格
    非正/非有限时，以 ``censor_pit_fact_missing`` 显式 censor。该信号只对
    已持仓者可执行：``existing_position_required=True``。
    """

    @property
    def detector_id(self) -> str:
        return DETECTOR_ID_S9

    @property
    def variant(self) -> str:
        return SIGNAL_VARIANT

    def detect(
        self, symbol: str, bars: Sequence[Bar], calendar: Sequence[date]
    ) -> tuple[Detection, ...]:
        ordered = _validated_bars(bars, calendar)
        if not ordered:
            return ()
        detections: list[Detection] = []
        for index, bar in enumerate(ordered):
            if index == 0:
                detections.append(
                    Detection(
                        self.detector_id,
                        self.variant,
                        symbol,
                        bar.date,
                        censor=EscapeCensorReason.PIT_FACT_MISSING,
                    )
                )
                continue
            prev_close = ordered[index - 1].quote_close_raw
            current_open = bar.quote_open_raw
            if (
                not math.isfinite(prev_close)
                or not math.isfinite(current_open)
                or prev_close <= 0
                or current_open <= 0
            ):
                detections.append(
                    Detection(
                        self.detector_id,
                        self.variant,
                        symbol,
                        bar.date,
                        censor=EscapeCensorReason.PIT_FACT_MISSING,
                    )
                )
                continue
            low_open_pct = (prev_close - current_open) / prev_close
            values: dict[str, object] = {
                "available_date": bar.date.isoformat(),
                "available_at_session": "open",
                "existing_position_required": True,
                "prev_date": ordered[index - 1].date.isoformat(),
                "prev_close": prev_close,
                "open": current_open,
                "low_open_pct": low_open_pct,
                "low_open_min_pct": LOW_OPEN_MIN_PCT,
                "hypothesis_label": HYPOTHESIS_LABEL,
            }
            detections.append(
                Detection(
                    self.detector_id,
                    self.variant,
                    symbol,
                    bar.date,
                    evidence=DetectionEvidence(low_open_pct >= LOW_OPEN_MIN_PCT, values),
                )
            )
        return tuple(detections)


@dataclass(frozen=True, slots=True)
class BaselineSeries:
    """调用方显式预计算的基线均值收益(按 N 交易日), 模块不伪造缺失基线。"""

    kind: str
    mean_return_by_horizon: Mapping[int, float]


@dataclass(frozen=True, slots=True)
class HorizonOutcomeStats:
    horizon: int
    events: int
    horizon_incomplete_events: int
    rise_events: int
    fall_events: int
    missed_escape_rate: float | None
    missed_gain_mean: float | None
    avoidance_depth_mean: float | None
    forward_return_mean: float | None
    net_forward_return_mean: float | None
    benchmark_status: str
    excess_return_mean: float | None


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    kind: str
    status: str
    baseline_return_mean_by_horizon: Mapping[int, float | None]
    net_vs_baseline_mean_by_horizon: Mapping[int, float | None]


@dataclass(frozen=True, slots=True)
class SignalResearch:
    signal_id: str
    detector_id: str
    capability: str
    verdict: str
    censor_codes: tuple[str, ...]
    horizons: tuple[HorizonOutcomeStats, ...]
    baselines: tuple[BaselineComparison, ...]


@dataclass(frozen=True, slots=True)
class SignalCountBucket:
    """同日同标的触发的不同信号计数分组; 仅计数, 无任何方向指令。"""

    signal_count: int
    events: int
    net_forward_return_mean_by_horizon: Mapping[int, float | None]


@dataclass(frozen=True, slots=True)
class EscapeRiskReport:
    horizons: tuple[int, ...]
    cost_bps: float
    round_trip_cost: float
    signals: tuple[SignalResearch, ...]
    count_buckets: tuple[SignalCountBucket, ...]
    unevaluated_events: int


@dataclass(slots=True)
class _Outcome:
    signal_id: str
    detection: Detection
    index: int
    forward: dict[int, float]
    net: dict[int, float]
    max_drawdown: dict[int, float]
    excess: dict[int, float]
    incomplete: set[int]
    benchmark_ok: dict[int, bool]


def _mean(values: Sequence[float]) -> float | None:
    if not values or not all(math.isfinite(value) for value in values):
        return None
    result = sum(values) / len(values)
    return result if math.isfinite(result) else None


def _max_drawdown(
    closes: Sequence[float],
    entry_index: int,
    horizon: int,
    entry_price: float,
) -> float:
    """Measure close-path drawdown from the first executable price."""
    peak = entry_price
    max_drawdown = 0.0
    for index in range(entry_index, entry_index + horizon):
        close = closes[index]
        peak = max(peak, close)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - close) / peak)
    return max_drawdown


def aggregate_escape_signals(
    detections: Sequence[Detection],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    cost_bps: float = COST_BPS_DEFAULT,
    baselines: Mapping[str, Mapping[str, BaselineSeries]] | None = None,
    benchmark: Mapping[str, Sequence[Bar]] | None = None,
    require_benchmark: bool = False,
    minute_approximation: bool = False,
) -> EscapeRiskReport:
    """对 S1/S8/S9 的 evidence 检测做逐信号独立研究聚合。

    - S1/S8 收盘确认后以下一交易日开盘为执行锚；S9 开盘确认且仅面向既有
      持仓，以信号日开盘为执行锚。N=1 表示执行日收盘。
    - 卖飞率 = 执行后 N 日收益为正的比例；规避深度 = 下跌事件的最大回撤
      均值。两方向对称呈现，不合并成方向指令。
    - 基线只接受显式 ``baselines``；缺失一律显式 unavailable。
    - ``minute_approximation=True`` 一律 ValueError：分钟信号不可用日线近似。
    """
    if minute_approximation:
        raise ValueError(
            "minute signals are unavailable_insufficient_immutable_history; "
            "daily high/low approximation is not accepted"
        )
    horizon_tuple = tuple(sorted({int(horizon) for horizon in horizons}))
    if not horizon_tuple or any(horizon < 1 for horizon in horizon_tuple):
        raise ValueError("horizons must be positive integers")
    if not 0 <= cost_bps <= COST_BPS_MAX:
        raise ValueError(f"cost_bps must be within [0, {COST_BPS_MAX}]")
    round_trip_cost = ROUND_TRIP_LEGS * cost_bps / 10_000
    symbol_bars = {symbol: tuple(bars) for symbol, bars in bars_by_symbol.items()}
    symbol_closes = {
        symbol: [bar.research_close_adj for bar in bars] for symbol, bars in symbol_bars.items()
    }
    symbol_positions = {
        symbol: {bar.date: index for index, bar in enumerate(bars)}
        for symbol, bars in symbol_bars.items()
    }
    benchmark_bars: dict[str, dict[date, Bar]] = {}
    for symbol, bench_rows in (benchmark or {}).items():
        benchmark_bars[symbol] = {bar.date: bar for bar in bench_rows}

    grouped: dict[str, list[Detection]] = {}
    censor_codes: dict[str, set[str]] = {signal_id: set() for signal_id in DAILY_SIGNAL_IDS}
    for detection in detections:
        signal_id = SIGNAL_ID_BY_DETECTOR.get(detection.detector_id)
        if signal_id is None:
            raise ValueError(f"unknown escape detector_id: {detection.detector_id}")
        if detection.censor is not None:
            censor_codes[signal_id].add(detection.censor.value)
            continue
        assert detection.evidence is not None
        grouped.setdefault(signal_id, []).append(detection)

    unevaluated_events = 0
    outcomes_by_signal: dict[str, list[_Outcome]] = {}
    co_occurrence: dict[tuple[str, date], set[str]] = {}
    for signal_id, signal_detections in grouped.items():
        outcomes: list[_Outcome] = []
        for detection in signal_detections:
            if not detection.evidence.qualified:
                continue
            co_occurrence.setdefault((detection.symbol, detection.signal_date), set()).add(
                signal_id
            )
            positions = symbol_positions.get(detection.symbol)
            closes = symbol_closes.get(detection.symbol)
            bars = symbol_bars.get(detection.symbol)
            signal_index = positions.get(detection.signal_date) if positions else None
            if closes is None or bars is None or signal_index is None:
                unevaluated_events += 1
                continue
            if signal_id == "s9":
                execution_index = signal_index
            else:
                execution_index = signal_index + 1
            if execution_index >= len(bars):
                unevaluated_events += 1
                continue
            entry = bars[execution_index].research_open_adj
            if not math.isfinite(entry) or entry <= 0:
                unevaluated_events += 1
                continue
            outcome = _Outcome(
                signal_id=signal_id,
                detection=detection,
                index=execution_index,
                forward={},
                net={},
                max_drawdown={},
                excess={},
                incomplete=set(),
                benchmark_ok={},
            )
            bench = benchmark_bars.get(detection.symbol)
            execution_date = bars[execution_index].date
            for horizon in horizon_tuple:
                exit_index = execution_index + horizon - 1
                if exit_index >= len(closes):
                    outcome.incomplete.add(horizon)
                    continue
                forward = closes[exit_index] / entry - 1
                outcome.forward[horizon] = forward
                outcome.net[horizon] = forward - round_trip_cost
                outcome.max_drawdown[horizon] = _max_drawdown(
                    closes,
                    execution_index,
                    horizon,
                    entry,
                )
                exit_date = bars[exit_index].date
                bench_entry = bench.get(execution_date) if bench else None
                bench_exit = bench.get(exit_date) if bench else None
                has_benchmark = (
                    bench_entry is not None
                    and bench_exit is not None
                    and bench_entry.research_open_adj > 0
                )
                outcome.benchmark_ok[horizon] = has_benchmark
                if has_benchmark:
                    outcome.excess[horizon] = forward - (
                        bench_exit.research_close_adj / bench_entry.research_open_adj - 1
                    )
            outcomes.append(outcome)
        outcomes_by_signal[signal_id] = outcomes

    signals: list[SignalResearch] = []
    for signal_id in DAILY_SIGNAL_IDS:
        detector_id = DETECTOR_ID_BY_SIGNAL[signal_id]
        outcomes = outcomes_by_signal.get(signal_id, [])
        horizon_stats: list[HorizonOutcomeStats] = []
        for horizon in horizon_tuple:
            evaluated = [outcome for outcome in outcomes if horizon in outcome.forward]
            forwards = [outcome.forward[horizon] for outcome in evaluated]
            nets = [outcome.net[horizon] for outcome in evaluated]
            rises = [value for value in forwards if value > 0]
            falls = [
                (outcome.forward[horizon], outcome.max_drawdown[horizon])
                for outcome in evaluated
                if outcome.forward[horizon] < 0
            ]
            excesses = [
                outcome.excess[horizon] for outcome in evaluated if horizon in outcome.excess
            ]
            benchmark_ok = (
                all(outcome.benchmark_ok.get(horizon, False) for outcome in evaluated)
                if evaluated
                else False
            )
            horizon_stats.append(
                HorizonOutcomeStats(
                    horizon=horizon,
                    events=len(evaluated),
                    horizon_incomplete_events=sum(
                        1 for outcome in outcomes if horizon in outcome.incomplete
                    ),
                    rise_events=len(rises),
                    fall_events=len(falls),
                    missed_escape_rate=(len(rises) / len(evaluated)) if evaluated else None,
                    missed_gain_mean=_mean(rises),
                    avoidance_depth_mean=(_mean([depth for _, depth in falls]) if falls else None),
                    forward_return_mean=_mean(forwards),
                    net_forward_return_mean=_mean(nets),
                    benchmark_status=(
                        BENCHMARK_STATUS_OK if benchmark_ok else BENCHMARK_STATUS_UNAVAILABLE
                    ),
                    excess_return_mean=_mean(excesses) if excesses else None,
                )
            )
        verdict = VERDICT_BASELINE_UNAVAILABLE if outcomes else VERDICT_NO_EVENTS
        codes = set(censor_codes[signal_id])
        if (
            require_benchmark
            and outcomes
            and any(
                not outcome.benchmark_ok.get(horizon, False)
                for outcome in outcomes
                for horizon in horizon_tuple
                if horizon in outcome.forward
            )
        ):
            verdict = VERDICT_BENCHMARK_MISSING
            codes.add(EscapeCensorReason.BENCHMARK_MISSING.value)
        signals.append(
            SignalResearch(
                signal_id=signal_id,
                detector_id=detector_id,
                capability=SIGNAL_CAPABILITIES[signal_id],
                verdict=verdict,
                censor_codes=tuple(sorted(codes)),
                horizons=tuple(horizon_stats),
                baselines=_baseline_comparisons(signal_id, horizon_stats, baselines),
            )
        )

    buckets: list[SignalCountBucket] = []
    count_groups: dict[int, list[tuple[str, date]]] = {}
    for key, signal_ids in co_occurrence.items():
        if len(signal_ids) >= 2:
            count_groups.setdefault(len(signal_ids), []).append(key)
    for count in sorted(count_groups):
        keys = count_groups[count]
        means: dict[int, float | None] = {}
        for horizon in horizon_tuple:
            nets = [
                outcome.net[horizon]
                for signal_outcomes in outcomes_by_signal.values()
                for outcome in signal_outcomes
                if (outcome.detection.symbol, outcome.detection.signal_date) in set(keys)
                and horizon in outcome.net
            ]
            means[horizon] = _mean(nets)
        buckets.append(
            SignalCountBucket(
                signal_count=count,
                events=len(keys),
                net_forward_return_mean_by_horizon=means,
            )
        )

    return EscapeRiskReport(
        horizons=horizon_tuple,
        cost_bps=cost_bps,
        round_trip_cost=round_trip_cost,
        signals=tuple(signals),
        count_buckets=tuple(buckets),
        unevaluated_events=unevaluated_events,
    )


def _baseline_comparisons(
    signal_id: str,
    horizon_stats: tuple[HorizonOutcomeStats, ...],
    baselines: Mapping[str, Mapping[str, BaselineSeries]] | None,
) -> tuple[BaselineComparison, ...]:
    stats_by_horizon = {stat.horizon: stat for stat in horizon_stats}
    provided = (baselines or {}).get(signal_id, {})
    comparisons: list[BaselineComparison] = []
    for kind in BASELINE_KINDS:
        series = provided.get(kind)
        if kind == "buy_hold":
            # buy_hold 由给定 bars 定义: signal 收盘持有 N 日, 即前向收益本身。
            baseline_means: dict[int, float | None] = {
                horizon: stats.forward_return_mean for horizon, stats in stats_by_horizon.items()
            }
            net_vs: dict[int, float | None] = {
                horizon: (
                    None
                    if stats.net_forward_return_mean is None or stats.forward_return_mean is None
                    else stats.net_forward_return_mean - stats.forward_return_mean
                )
                for horizon, stats in stats_by_horizon.items()
            }
            comparisons.append(
                BaselineComparison(
                    kind=kind,
                    status=BASELINE_STATUS_OK,
                    baseline_return_mean_by_horizon=baseline_means,
                    net_vs_baseline_mean_by_horizon=net_vs,
                )
            )
            continue
        if series is None:
            comparisons.append(
                BaselineComparison(
                    kind=kind,
                    status=BASELINE_STATUS_UNAVAILABLE,
                    baseline_return_mean_by_horizon={horizon: None for horizon in stats_by_horizon},
                    net_vs_baseline_mean_by_horizon={horizon: None for horizon in stats_by_horizon},
                )
            )
            continue
        baseline_means = {
            horizon: series.mean_return_by_horizon.get(horizon) for horizon in stats_by_horizon
        }
        net_vs = {
            horizon: (
                None
                if value is None or stats_by_horizon[horizon].net_forward_return_mean is None
                else stats_by_horizon[horizon].net_forward_return_mean - value
            )
            for horizon, value in baseline_means.items()
        }
        comparisons.append(
            BaselineComparison(
                kind=kind,
                status=BASELINE_STATUS_OK,
                baseline_return_mean_by_horizon=baseline_means,
                net_vs_baseline_mean_by_horizon=net_vs,
            )
        )
    return tuple(comparisons)


__all__ = [
    "BASELINE_KINDS",
    "BaselineComparison",
    "BaselineSeries",
    "DAILY_SIGNAL_IDS",
    "DEFAULT_HORIZONS",
    "DETECTOR_ID_S1",
    "DETECTOR_ID_S8",
    "DETECTOR_ID_S9",
    "EscapeCensorReason",
    "EscapeRiskReport",
    "EscapeS1Detector",
    "EscapeS8Detector",
    "EscapeS9Detector",
    "HorizonOutcomeStats",
    "LOW_OPEN_MIN_PCT",
    "MACD_HIST_SCALE",
    "MACD_MIN_VALID_INDEX",
    "MINUTE_SIGNAL_IDS",
    "NEW_HIGH_WINDOW_DAYS",
    "SIGNAL_CAPABILITIES",
    "SIGNAL_CAPABILITY_AVAILABLE",
    "SIGNAL_CAPABILITY_MINUTE_UNAVAILABLE",
    "SIGNAL_VARIANT",
    "SignalCountBucket",
    "SignalResearch",
    "THREE_YIN_DAYS",
    "aggregate_escape_signals",
    "capability_for",
    "macd_histogram",
    "require_daily_signal",
]
