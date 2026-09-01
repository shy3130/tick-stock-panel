"""D0 pure adjusted-price candle morphology."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from app.services.hold_firm_patterns.models import Bar

from .models import (
    DOJI_BODY_RATIO_MAX,
    DOJI_SHADOW_BODY_MULT,
    DOJI_VOLUME_EXPAND_MIN,
    DOJI_VOLUME_SHRINK_MAX,
    VolumeState,
)


@dataclass(frozen=True, slots=True)
class CandleMetrics:
    body: float
    range: float
    upper: float
    lower: float
    body_ratio: float | None


def candle_metrics(bar: Bar) -> CandleMetrics:
    body = abs(bar.research_close_adj - bar.research_open_adj)
    span = bar.research_high_adj - bar.research_low_adj
    ratio = body / span if span > 0 and math.isfinite(span) else None
    return CandleMetrics(
        body,
        span,
        bar.research_high_adj - max(bar.research_open_adj, bar.research_close_adj),
        min(bar.research_open_adj, bar.research_close_adj) - bar.research_low_adj,
        ratio,
    )


def is_doji(bar: Bar, *, theta: float = DOJI_BODY_RATIO_MAX) -> bool:
    return (m := candle_metrics(bar)).body_ratio is not None and m.body_ratio <= theta


def is_gravestone(bar: Bar, *, theta: float = DOJI_BODY_RATIO_MAX) -> bool:
    m = candle_metrics(bar)
    return (
        m.body_ratio is not None
        and m.body_ratio <= theta
        and m.upper >= DOJI_SHADOW_BODY_MULT * m.body + m.lower
    )


def is_t_bar(bar: Bar, *, theta: float = DOJI_BODY_RATIO_MAX) -> bool:
    m = candle_metrics(bar)
    return (
        m.body_ratio is not None
        and m.body_ratio <= theta
        and m.lower >= DOJI_SHADOW_BODY_MULT * m.body + m.upper
    )


def position_percentile(window: Sequence[Bar]) -> float | None:
    if not window:
        return None
    lo = min(x.research_low_adj for x in window)
    hi = max(x.research_high_adj for x in window)
    span = hi - lo
    return (
        None
        if span <= 0 or not math.isfinite(span)
        else (window[-1].research_close_adj - lo) / span
    )


def prior_move_pct(window: Sequence[Bar]) -> float | None:
    if len(window) < 2 or window[0].research_close_adj <= 0:
        return None
    return window[-1].research_close_adj / window[0].research_close_adj - 1


def reference_volume(window: Sequence[Bar]) -> float | None:
    if not window or any(x.volume <= 0 or not math.isfinite(x.volume) for x in window):
        return None
    return sum(x.volume for x in window) / len(window)


def volume_state(volume: float, reference_mean: float | None) -> VolumeState | None:
    if reference_mean is None or reference_mean <= 0 or not math.isfinite(reference_mean):
        return None
    ratio = volume / reference_mean
    return (
        VolumeState.SHRINK
        if ratio <= DOJI_VOLUME_SHRINK_MAX
        else VolumeState.EXPAND
        if ratio >= DOJI_VOLUME_EXPAND_MIN
        else VolumeState.FLAT
    )


def validate_series(
    bars: Sequence[Bar], calendar: Sequence[date]
) -> tuple[tuple[date, ...], dict[date, Bar]]:
    cal = tuple(calendar)
    if len(set(cal)) != len(cal) or any(cal[i] >= cal[i + 1] for i in range(len(cal) - 1)):
        raise ValueError("calendar must be ascending and unique")
    out = {}
    prev = None
    for bar in bars:
        if prev is not None and bar.date <= prev:
            raise ValueError("bars must be ascending")
        if bar.date not in cal:
            raise ValueError("bar date missing from calendar")
        out[bar.date] = bar
        prev = bar.date
    return cal, out
