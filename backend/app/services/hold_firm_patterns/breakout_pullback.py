"""F2 ``breakout_pullback`` detector for Issue #38 hold-firm-pattern research.

Contract: ``docs/ISSUE-38/final-design.md`` §4 (selection landmark & common
clock) and §5 (F2).

Parent event: a 20-complete-day platform with
``(max(high_adj) - min(low_adj)) / min(low_adj) < 0.15`` whose day closes
strictly above the platform high with breakout volume ``>= 1.50x`` the
prior-20-day mean volume. ``level = prior platform high``.

The selection landmark is the close of market day 5 after the breakout. The
first pullback day inside days 1..5 satisfying ``low_adj <= level*1.01``,
``close_adj >= level`` and ``volume <= 0.70x breakout_volume`` never executes
early: the event classifies only once the full day-5 window is on record,
otherwise it is censored as ``censor_selection_window_incomplete``.

OLS ``log(volume)`` slope over the pullback window and the fake-breakout
window (market days 6..10, any close strictly below level) are diagnostics
only and never enter the selection mask. Pure detection: adjusted OHLC and
volume only, no I/O, no returns, no trading advice.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Sequence, cast

from .models import (
    Bar,
    CensorReason,
    DEFINITION_VERSION,
    DetectionEvidence,
    FACTOR_IDS,
    FactorId,
    Landmark,
    LandmarkKind,
    MarketFactsSource,
    ParentDetection,
)

PLATFORM_DAYS = 20
PLATFORM_RANGE_RATIO_MAX = 0.15
BREAKOUT_VOLUME_RATIO_MIN = 1.50
PULLBACK_WINDOW_DAYS = 5
PULLBACK_LOW_RATIO_MAX = 1.01
PULLBACK_VOLUME_RATIO_MAX = 0.70
FAKE_BREAKOUT_WINDOW_DAYS = 5

FAKE_WINDOW_COMPLETE = "complete"
FAKE_WINDOW_CENSORED = CensorReason.DIAGNOSTIC_WINDOW_INCOMPLETE.value


def _log_volume_slope(volumes: Sequence[float]) -> float | None:
    """OLS slope of log(volume) over the day 1..5 window; diagnostic only."""
    if len(volumes) < 2 or any(volume <= 0.0 for volume in volumes):
        return None
    logs = [math.log(volume) for volume in volumes]
    count = len(logs)
    mean_t = (count - 1) / 2.0
    mean_y = sum(logs) / count
    numerator = sum((t - mean_t) * (y - mean_y) for t, y in enumerate(logs))
    denominator = sum((t - mean_t) ** 2 for t in range(count))
    if denominator == 0.0:
        return None
    return numerator / denominator


def _bars_between(
    bars: Sequence[Bar], begin: int, first_day: date, last_day: date
) -> dict[date, Bar]:
    """Index bars in ``[first_day, last_day]`` by date, scanning from ``begin``."""
    found: dict[date, Bar] = {}
    for j in range(begin, len(bars)):
        bar = bars[j]
        if bar.date > last_day:
            break
        if bar.date >= first_day:
            found[bar.date] = bar
    return found


class BreakoutPullbackDetector:
    """F2: platform breakout plus first volume-shrinking pullback by day 5."""

    factor_id: FactorId = cast(FactorId, FACTOR_IDS[1])

    def detect(
        self,
        symbol: str,
        bars: Sequence[Bar],
        facts: MarketFactsSource,
        calendar: Sequence[date],
    ) -> tuple[ParentDetection, ...]:
        ordered = tuple(bars)
        cal = tuple(calendar)
        if not ordered or not cal:
            return ()
        cal_index = {day: idx for idx, day in enumerate(cal)}
        for idx in range(len(cal) - 1):
            if cal[idx] >= cal[idx + 1]:
                raise ValueError("calendar must be strictly ascending")
        previous: date | None = None
        for bar in ordered:
            if previous is not None and bar.date <= previous:
                raise ValueError("bars must be strictly ascending by date")
            previous = bar.date
            if bar.date not in cal_index:
                raise ValueError(f"bar date {bar.date} missing from calendar")

        events: list[ParentDetection] = []
        bar_by_date = {bar.date: bar for bar in ordered}
        # Detector-level suppression only covers the fixed day-5 selection
        # window. The evaluator enforces the longer active/pending horizon.
        blocked_through = -1
        for breakout in ordered:
            breakout_cal_idx = cal_index[breakout.date]
            if breakout_cal_idx <= blocked_through:
                continue
            if breakout_cal_idx < PLATFORM_DAYS:
                continue
            platform_days = cal[breakout_cal_idx - PLATFORM_DAYS : breakout_cal_idx]
            if len(platform_days) != PLATFORM_DAYS or any(
                day not in bar_by_date for day in platform_days
            ):
                # The observable parent itself cannot be established.
                continue
            platform = [bar_by_date[day] for day in platform_days]
            platform_high = max(bar.research_high_adj for bar in platform)
            platform_low = min(bar.research_low_adj for bar in platform)
            if platform_low <= 0.0:
                continue
            range_ratio = (platform_high - platform_low) / platform_low
            if not range_ratio < PLATFORM_RANGE_RATIO_MAX:
                continue
            if not breakout.research_close_adj > platform_high:
                continue
            prior_mean_volume = sum(bar.volume for bar in platform) / PLATFORM_DAYS
            volume_ratio = breakout.volume / prior_mean_volume if prior_mean_volume > 0.0 else None
            volume_expanded = (
                prior_mean_volume > 0.0
                and breakout.volume >= prior_mean_volume * BREAKOUT_VOLUME_RATIO_MIN
            )
            last_window_idx = breakout_cal_idx + PULLBACK_WINDOW_DAYS
            window_cal = cal[breakout_cal_idx + 1 : last_window_idx + 1]
            if len(window_cal) < PULLBACK_WINDOW_DAYS:
                events.append(
                    ParentDetection(
                        self.factor_id,
                        symbol,
                        breakout.date,
                        None,
                        censor=CensorReason.SELECTION_WINDOW_INCOMPLETE,
                    )
                )
                # Calendar ended inside the selection window; nothing later
                # can ever complete, so suppress all remaining candidates.
                blocked_through = len(cal)
                continue
            day5 = window_cal[-1]
            landmark = Landmark(LandmarkKind.BREAKOUT_DAY5_CLOSE, breakout.date, day5)
            window_bars = {day: bar_by_date.get(day) for day in window_cal}
            if any(bar is None for bar in window_bars.values()):
                events.append(
                    ParentDetection(
                        self.factor_id,
                        symbol,
                        breakout.date,
                        landmark,
                        censor=CensorReason.SELECTION_WINDOW_INCOMPLETE,
                    )
                )
                blocked_through = last_window_idx
                continue

            hit = None
            for day_index, window_day in enumerate(window_cal, start=1):
                window_bar = window_bars[window_day]
                assert window_bar is not None
                low_level_ratio = window_bar.research_low_adj / platform_high
                volume_shrink_ratio = (
                    window_bar.volume / breakout.volume if breakout.volume > 0.0 else None
                )
                if (
                    window_bar.research_low_adj <= platform_high * PULLBACK_LOW_RATIO_MAX
                    and window_bar.research_close_adj >= platform_high
                    and window_bar.volume <= breakout.volume * PULLBACK_VOLUME_RATIO_MAX
                ):
                    hit = {
                        "day_index": day_index,
                        "date": window_day.isoformat(),
                        "low_adj": window_bar.research_low_adj,
                        "low_level_ratio": low_level_ratio,
                        "close_adj": window_bar.research_close_adj,
                        "volume": window_bar.volume,
                        "volume_vs_breakout_ratio": volume_shrink_ratio,
                    }
                    break

            window_volumes = [
                window_bars[window_day].volume
                for window_day in window_cal
                if window_bars[window_day] is not None
            ]
            ols_slope = _log_volume_slope(window_volumes)

            fake_start_idx = last_window_idx + 1
            fake_cal = cal[fake_start_idx : fake_start_idx + FAKE_BREAKOUT_WINDOW_DAYS]
            fake_block: dict[str, object] = {
                "window_days": FAKE_BREAKOUT_WINDOW_DAYS,
                "window_start": fake_cal[0].isoformat() if fake_cal else None,
                "window_end": fake_cal[-1].isoformat()
                if len(fake_cal) == FAKE_BREAKOUT_WINDOW_DAYS
                else None,
                "threshold_close_adj_strictly_below_level": platform_high,
                "status": FAKE_WINDOW_CENSORED,
                "fake_breakout": None,
                "min_close_adj": None,
                "first_breach_date": None,
            }
            if len(fake_cal) == FAKE_BREAKOUT_WINDOW_DAYS:
                fake_bars = {day: bar_by_date.get(day) for day in fake_cal}
                if all(bar is not None for bar in fake_bars.values()):
                    closes = [
                        fake_bars[window_day].research_close_adj
                        for window_day in fake_cal
                        if fake_bars[window_day] is not None
                    ]
                    breaches = [
                        window_day
                        for window_day, close_adj in zip(fake_cal, closes)
                        if close_adj < platform_high
                    ]
                    fake_block.update(
                        status=FAKE_WINDOW_COMPLETE,
                        fake_breakout=bool(breaches),
                        min_close_adj=min(closes),
                        first_breach_date=breaches[0].isoformat() if breaches else None,
                    )

            evidence = DetectionEvidence(
                qualified=volume_expanded and hit is not None,
                values={
                    "definition_version": DEFINITION_VERSION,
                    "platform": {
                        "window_days": PLATFORM_DAYS,
                        "range_ratio_threshold_max": PLATFORM_RANGE_RATIO_MAX,
                        "range_ratio": range_ratio,
                        "high_adj": platform_high,
                        "low_adj": platform_low,
                    },
                    "breakout": {
                        "date": breakout.date.isoformat(),
                        "close_adj": breakout.research_close_adj,
                        "level_adj": platform_high,
                        "close_condition": "close_adj > level_adj (strict)",
                        "volume": breakout.volume,
                        "prior_20d_mean_volume": prior_mean_volume,
                        "volume_ratio_threshold_min": BREAKOUT_VOLUME_RATIO_MIN,
                        "volume_ratio": volume_ratio,
                    },
                    "pullback": {
                        "search_window_days": PULLBACK_WINDOW_DAYS,
                        "level_adj": platform_high,
                        "low_level_ratio_threshold_max": PULLBACK_LOW_RATIO_MAX,
                        "volume_ratio_threshold_max": PULLBACK_VOLUME_RATIO_MAX,
                        "close_condition": "close_adj >= level_adj (inclusive)",
                        "hit": hit is not None,
                        **(hit or {}),
                    },
                    "landmark": {
                        "kind": LandmarkKind.BREAKOUT_DAY5_CLOSE.value,
                        "anchor_date": breakout.date.isoformat(),
                        "landmark_date": day5.isoformat(),
                    },
                    "diagnostics": {
                        "ols_window": "selection days 1..5",
                        "ols_log_volume_slope": ols_slope,
                        "ols_slope_negative": ols_slope < 0 if ols_slope is not None else None,
                        "ols_mask_note": "diagnostic only; slope<0 never enters the selection mask",
                        "fake_breakout": fake_block,
                    },
                },
            )
            events.append(
                ParentDetection(
                    self.factor_id,
                    symbol,
                    breakout.date,
                    landmark,
                    evidence=evidence,
                )
            )
            blocked_through = last_window_idx
        return tuple(events)
