from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

TrendBias = Literal["BULLISH", "BEARISH", "NEUTRAL", "TRANSITION"]
OpportunityChange = Literal[
    "STRENGTHENING",
    "WEAKENING",
    "UNCHANGED",
    "REVERSING",
]
ChannelDirection = Literal["UP", "DOWN", "RANGE", "TRANSITION"]
PatternStatus = Literal["FORMING", "CONFIRMED", "FAILED", "NONE"]

MIN_STAGE_BARS = 5
CHANNEL_SLOPE_PCT = 0.2
RANGE_SLOPE_PCT = 0.15
RANGE_WIDTH_PCT = 1.0
BREAKOUT_HOLD_PCT = 0.05
V_RECOVERY_RATIO = 0.5
OPPORTUNITY_CHANGE_DELTA = 0.25


class PreviousStageContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trend_bias: TrendBias
    opportunity_score: float


class StageAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bar_count: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float
    vwap: float | None
    change_pct: float | None
    range_pct: float | None
    close_position: float | None
    high_time: datetime | None
    low_time: datetime | None
    max_consecutive_up: int
    max_consecutive_down: int


class StagePathSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    change_pct: float


class ChannelCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: ChannelDirection
    slope_pct: float
    evidence_metric_keys: tuple[str, ...]


class PatternCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    status: PatternStatus
    pivot_time: datetime | None = None
    evidence_metric_keys: tuple[str, ...]
    invalidation_metric_keys: tuple[str, ...] = ()


class VolumeAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_half_volume: float
    second_half_volume: float
    final_five_minute_share: float
    direction: Literal["EXPANSION_UP", "EXPANSION_DOWN", "LATE_VOLUME", "BALANCED"]


class HourlyMarketStructure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_start: datetime
    data_cutoff: datetime
    stage: StageAggregate
    cumulative: StageAggregate
    path_segments: tuple[StagePathSegment, ...]
    channel: ChannelCandidate
    patterns: tuple[PatternCandidate, ...]
    volume: VolumeAssessment
    trend_bias: TrendBias
    opportunity_score: float
    opportunity_change: OpportunityChange
    hidden_events: tuple[str, ...]
    evidence_values: dict[str, float]
    data_quality: tuple[str, ...]


class _MinuteRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def build_hourly_market_structure(
    *,
    minute_rows: Sequence[Mapping[str, Any]],
    stage_start: datetime,
    data_cutoff: datetime,
    previous_stage: PreviousStageContext | None,
) -> HourlyMarketStructure:
    normalized = _normalize_rows(minute_rows, data_cutoff=data_cutoff)
    stage_rows = [row for row in normalized if row.observed_at >= stage_start]
    cumulative = _aggregate(normalized)
    stage = _aggregate(stage_rows)
    evidence = _evidence(stage)
    path_segments = tuple(
        _segment(chunk)
        for index in range(0, len(stage_rows), 5)
        if (chunk := stage_rows[index : index + 5])
    )
    channel = _channel(stage_rows, stage)
    patterns = _patterns(
        stage_rows=stage_rows,
        pre_stage_rows=[row for row in normalized if row.observed_at < stage_start],
        stage=stage,
        evidence=evidence,
    )
    volume = _volume(stage_rows, stage)
    trend_bias = _trend_bias(channel.direction)
    opportunity_score = _opportunity_score(
        trend_bias=trend_bias,
        stage=stage,
        patterns=patterns,
        volume=volume,
    )
    opportunity_change = classify_opportunity_change(
        trend_bias,
        opportunity_score,
        previous_stage,
    )
    quality = () if len(stage_rows) >= MIN_STAGE_BARS else (
        f"有效阶段分钟不足 {MIN_STAGE_BARS} 条",
    )
    return HourlyMarketStructure(
        stage_start=stage_start,
        data_cutoff=data_cutoff,
        stage=stage,
        cumulative=cumulative,
        path_segments=path_segments,
        channel=channel,
        patterns=tuple(patterns),
        volume=volume,
        trend_bias=trend_bias,
        opportunity_score=opportunity_score,
        opportunity_change=opportunity_change,
        hidden_events=_hidden_events(stage, patterns, volume),
        evidence_values=evidence,
        data_quality=quality,
    )


def classify_opportunity_change(
    current_bias: str,
    current_score: float,
    previous_stage: PreviousStageContext | None,
) -> OpportunityChange:
    if previous_stage is None:
        return "UNCHANGED"
    if (
        current_bias in {"BULLISH", "BEARISH"}
        and previous_stage.trend_bias in {"BULLISH", "BEARISH"}
        and current_bias != previous_stage.trend_bias
    ):
        return "REVERSING"
    delta = current_score - previous_stage.opportunity_score
    if delta >= OPPORTUNITY_CHANGE_DELTA:
        return "STRENGTHENING"
    if delta <= -OPPORTUNITY_CHANGE_DELTA:
        return "WEAKENING"
    return "UNCHANGED"


def _normalize_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    data_cutoff: datetime,
) -> list[_MinuteRow]:
    by_time: dict[datetime, _MinuteRow] = {}
    for raw in rows:
        observed_at = _datetime(raw.get("decision_minute") or raw.get("observed_at"))
        if observed_at is None or observed_at > data_cutoff:
            continue
        open_price = _number(raw.get("minute_open") or raw.get("open"))
        high = _number(raw.get("minute_high") or raw.get("high"))
        low = _number(raw.get("minute_low") or raw.get("low"))
        close = _number(raw.get("minute_close") or raw.get("last_price") or raw.get("close"))
        volume = _number(raw.get("minute_volume") or raw.get("volume"))
        if None in {open_price, high, low, close}:
            continue
        assert open_price is not None and high is not None and low is not None and close is not None
        by_time[observed_at] = _MinuteRow(
            observed_at=observed_at,
            open=open_price,
            high=max(high, open_price, close),
            low=min(low, open_price, close),
            close=close,
            volume=max(volume or 0.0, 0.0),
        )
    return [by_time[key] for key in sorted(by_time)]


def _aggregate(rows: Sequence[_MinuteRow]) -> StageAggregate:
    if not rows:
        return StageAggregate(
            bar_count=0,
            open=None,
            high=None,
            low=None,
            close=None,
            volume=0,
            vwap=None,
            change_pct=None,
            range_pct=None,
            close_position=None,
            high_time=None,
            low_time=None,
            max_consecutive_up=0,
            max_consecutive_down=0,
        )
    opened = rows[0].open
    high_row = max(rows, key=lambda item: item.high)
    low_row = min(rows, key=lambda item: item.low)
    closed = rows[-1].close
    volume = sum(row.volume for row in rows)
    vwap = (
        sum(row.close * row.volume for row in rows) / volume
        if volume > 0
        else sum(row.close for row in rows) / len(rows)
    )
    width = high_row.high - low_row.low
    return StageAggregate(
        bar_count=len(rows),
        open=opened,
        high=high_row.high,
        low=low_row.low,
        close=closed,
        volume=volume,
        vwap=vwap,
        change_pct=(closed / opened - 1) * 100 if opened else None,
        range_pct=width / opened * 100 if opened else None,
        close_position=(closed - low_row.low) / width if width > 0 else 0.5,
        high_time=high_row.observed_at,
        low_time=low_row.observed_at,
        max_consecutive_up=_max_run(rows, rising=True),
        max_consecutive_down=_max_run(rows, rising=False),
    )


def _max_run(rows: Sequence[_MinuteRow], *, rising: bool) -> int:
    best = current = 0
    previous = rows[0].open
    for row in rows:
        moved = row.close > previous if rising else row.close < previous
        current = current + 1 if moved else 0
        best = max(best, current)
        previous = row.close
    return best


def _segment(rows: Sequence[_MinuteRow]) -> StagePathSegment:
    aggregate = _aggregate(rows)
    assert aggregate.open is not None
    assert aggregate.high is not None
    assert aggregate.low is not None
    assert aggregate.close is not None
    return StagePathSegment(
        start=rows[0].observed_at,
        end=rows[-1].observed_at,
        open=aggregate.open,
        high=aggregate.high,
        low=aggregate.low,
        close=aggregate.close,
        volume=aggregate.volume,
        change_pct=aggregate.change_pct or 0.0,
    )


def _channel(rows: Sequence[_MinuteRow], stage: StageAggregate) -> ChannelCandidate:
    slope = stage.change_pct or 0.0
    if len(rows) < 3:
        direction: ChannelDirection = "TRANSITION"
    else:
        buckets = [
            chunk
            for index in range(3)
            if (chunk := rows[index * len(rows) // 3 : (index + 1) * len(rows) // 3])
        ]
        highs = [max(row.high for row in chunk) for chunk in buckets]
        lows = [min(row.low for row in chunk) for chunk in buckets]
        higher = all(right > left for left, right in pairwise(highs)) and all(
            right > left for left, right in pairwise(lows)
        )
        lower = all(right < left for left, right in pairwise(highs)) and all(
            right < left for left, right in pairwise(lows)
        )
        if higher and slope >= CHANNEL_SLOPE_PCT:
            direction = "UP"
        elif lower and slope <= -CHANNEL_SLOPE_PCT:
            direction = "DOWN"
        elif abs(slope) < RANGE_SLOPE_PCT and (stage.range_pct or 0.0) < RANGE_WIDTH_PCT:
            direction = "RANGE"
        else:
            direction = "TRANSITION"
    return ChannelCandidate(
        direction=direction,
        slope_pct=slope,
        evidence_metric_keys=("stage.change_pct", "stage.range_pct", "stage.close_position"),
    )


def _patterns(
    *,
    stage_rows: Sequence[_MinuteRow],
    pre_stage_rows: Sequence[_MinuteRow],
    stage: StageAggregate,
    evidence: dict[str, float],
) -> list[PatternCandidate]:
    patterns: list[PatternCandidate] = []
    if stage_rows and stage.open and stage.low is not None and stage.close is not None and stage.vwap is not None:
        low_index = min(range(len(stage_rows)), key=lambda index: stage_rows[index].low)
        drop = stage.open - stage.low
        recovery = stage.close - stage.low
        ratio = recovery / drop if drop > 0 else 0.0
        evidence["stage.v_recovery_ratio"] = ratio
        if low_index < len(stage_rows) * 2 / 3 and ratio >= V_RECOVERY_RATIO and stage.close > stage.vwap:
            patterns.append(
                PatternCandidate(
                    kind="V_REPAIR",
                    status="CONFIRMED",
                    pivot_time=stage_rows[low_index].observed_at,
                    evidence_metric_keys=(
                        "stage.low",
                        "stage.close",
                        "stage.vwap",
                        "stage.v_recovery_ratio",
                    ),
                    invalidation_metric_keys=("stage.low", "stage.vwap"),
                )
            )
    if stage_rows and stage.open and stage.high is not None and stage.close is not None and stage.vwap is not None:
        high_index = max(range(len(stage_rows)), key=lambda index: stage_rows[index].high)
        rise = stage.high - stage.open
        giveback = stage.high - stage.close
        ratio = giveback / rise if rise > 0 else 0.0
        evidence["stage.inverted_v_giveback_ratio"] = ratio
        if high_index < len(stage_rows) * 2 / 3 and ratio >= V_RECOVERY_RATIO and stage.close < stage.vwap:
            patterns.append(
                PatternCandidate(
                    kind="INVERTED_V",
                    status="CONFIRMED",
                    pivot_time=stage_rows[high_index].observed_at,
                    evidence_metric_keys=(
                        "stage.high",
                        "stage.close",
                        "stage.vwap",
                        "stage.inverted_v_giveback_ratio",
                    ),
                    invalidation_metric_keys=("stage.high", "stage.vwap"),
                )
            )
    if pre_stage_rows and stage.high is not None and stage.close is not None:
        pre_high = max(row.high for row in pre_stage_rows)
        pre_low = min(row.low for row in pre_stage_rows)
        evidence["reference.pre_stage_high"] = pre_high
        evidence["reference.pre_stage_low"] = pre_low
        if stage.high > pre_high:
            held = stage.close >= pre_high * (1 + BREAKOUT_HOLD_PCT / 100)
            patterns.append(
                PatternCandidate(
                    kind="BREAKOUT" if held else "FALSE_BREAKOUT",
                    status="CONFIRMED",
                    evidence_metric_keys=(
                        "reference.pre_stage_high",
                        "stage.high",
                        "stage.close",
                    ),
                    invalidation_metric_keys=("reference.pre_stage_high",),
                )
            )
        elif stage.low is not None and stage.low < pre_low:
            held = stage.close <= pre_low * (1 - BREAKOUT_HOLD_PCT / 100)
            patterns.append(
                PatternCandidate(
                    kind="BREAKDOWN" if held else "FALSE_BREAKDOWN",
                    status="CONFIRMED",
                    evidence_metric_keys=(
                        "reference.pre_stage_low",
                        "stage.low",
                        "stage.close",
                    ),
                    invalidation_metric_keys=("reference.pre_stage_low",),
                )
            )
    if not patterns:
        patterns.append(
            PatternCandidate(
                kind="NONE",
                status="NONE",
                evidence_metric_keys=("stage.change_pct", "stage.range_pct"),
            )
        )
    return patterns


def _volume(rows: Sequence[_MinuteRow], stage: StageAggregate) -> VolumeAssessment:
    midpoint = len(rows) // 2
    first = sum(row.volume for row in rows[:midpoint])
    second = sum(row.volume for row in rows[midpoint:])
    final_five = sum(row.volume for row in rows[-5:])
    share = final_five / stage.volume if stage.volume else 0.0
    change = stage.change_pct or 0.0
    if second > first * 1.2 and change > 0:
        direction = "EXPANSION_UP"
    elif second > first * 1.2 and change < 0:
        direction = "EXPANSION_DOWN"
    elif share >= 0.5:
        direction = "LATE_VOLUME"
    else:
        direction = "BALANCED"
    return VolumeAssessment(
        first_half_volume=first,
        second_half_volume=second,
        final_five_minute_share=share,
        direction=direction,
    )


def _trend_bias(direction: ChannelDirection) -> TrendBias:
    return {
        "UP": "BULLISH",
        "DOWN": "BEARISH",
        "RANGE": "NEUTRAL",
        "TRANSITION": "TRANSITION",
    }[direction]


def _opportunity_score(
    *,
    trend_bias: TrendBias,
    stage: StageAggregate,
    patterns: Sequence[PatternCandidate],
    volume: VolumeAssessment,
) -> float:
    score = {
        "BULLISH": 0.45,
        "BEARISH": -0.45,
        "NEUTRAL": 0.0,
        "TRANSITION": 0.0,
    }[trend_bias]
    kinds = {pattern.kind for pattern in patterns}
    if "BREAKOUT" in kinds or "V_REPAIR" in kinds:
        score += 0.25
    if "BREAKDOWN" in kinds or "INVERTED_V" in kinds:
        score -= 0.25
    if volume.direction == "EXPANSION_UP":
        score += 0.15
    elif volume.direction == "EXPANSION_DOWN":
        score -= 0.15
    if stage.close_position is not None:
        score += (stage.close_position - 0.5) * 0.2
    return max(-1.0, min(1.0, score))


def _evidence(stage: StageAggregate) -> dict[str, float]:
    values: dict[str, float] = {"stage.volume": stage.volume}
    for key in ("open", "high", "low", "close", "vwap", "change_pct", "range_pct", "close_position"):
        value = getattr(stage, key)
        if value is not None:
            values[f"stage.{key}"] = value
    return values


def _hidden_events(
    stage: StageAggregate,
    patterns: Sequence[PatternCandidate],
    volume: VolumeAssessment,
) -> tuple[str, ...]:
    events = [pattern.kind for pattern in patterns if pattern.kind != "NONE"]
    if stage.max_consecutive_up >= 3:
        events.append("CONSECUTIVE_UP")
    if stage.max_consecutive_down >= 3:
        events.append("CONSECUTIVE_DOWN")
    if volume.final_five_minute_share >= 0.5:
        events.append("LATE_VOLUME_CONCENTRATION")
    return tuple(events)


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
