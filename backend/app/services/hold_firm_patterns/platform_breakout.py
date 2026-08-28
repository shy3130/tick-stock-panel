"""F4 ``bottom_platform_breakout`` detector for Issue #38 hold-firm research.

Frozen definition (docs/ISSUE-38/final-design.md §5 F4):

- parent event: prior 20 complete days form a platform with amplitude
  ``(max(high_adj) - min(low_adj)) / min(low_adj) < 0.15`` and the signal-day
  ``close_adj`` closes strictly above ``platform_high_adj``;
- qualified: additionally the bottom position over the 120 days before the
  platform start is ``<= 0.35`` (algorithm identical to F3), the same-day
  body gain ``close_adj / open_adj - 1 >= 0.05`` and
  ``volume >= 1.50 x prior_20d_mean_volume``; otherwise ``not_selected``;
- landmark: the signal-day close (``LandmarkKind.SIGNAL_DAY_CLOSE``).

Detection is pure over the already-loaded bar sequence: no I/O, no market
facts access, no returns, and the future five diagnostic days are never read
here. Evidence carries the machine-readable thresholds, the observed values,
and the anchors the evaluator needs for the five-day strength buckets
(``entity_bottom_adj`` / ``breakout_close_adj``) and the fake-breakout check
(``platform_high_adj``). Diagnostics (e.g. platform amplitude) are recorded
but never feed back into the qualified mask.
"""

from __future__ import annotations
import math

from datetime import date
from typing import Sequence

from . import models

FACTOR_ID: models.FactorId = "bottom_platform_breakout"

BOTTOM_WINDOW_DAYS = 120
PLATFORM_WINDOW_DAYS = 20
BOTTOM_POSITION_THRESHOLD = 0.35
PLATFORM_AMPLITUDE_THRESHOLD = 0.15
SAME_DAY_GAIN_THRESHOLD = 0.05
VOLUME_RATIO_THRESHOLD = 1.5


class PlatformBreakoutDetector:
    """Emit one :class:`models.ParentDetection` per platform-breakout day."""

    factor_id: models.FactorId = FACTOR_ID

    def detect(
        self,
        symbol: str,
        bars: Sequence[models.Bar],
        facts: models.MarketFactsSource,
        calendar: Sequence[date],
    ) -> tuple[models.ParentDetection, ...]:
        del facts
        ordered = tuple(bars)
        cal = tuple(calendar)
        if not ordered or not cal:
            return ()
        cal_index = {day: index for index, day in enumerate(cal)}
        bar_by_date = {bar.date: bar for bar in ordered}
        if len(cal_index) != len(cal):
            raise ValueError("calendar dates must be unique")
        if any(cal[index] >= cal[index + 1] for index in range(len(cal) - 1)):
            raise ValueError("calendar must be strictly ascending")
        previous: date | None = None
        for bar in ordered:
            if previous is not None and bar.date <= previous:
                raise ValueError("bars must be strictly ascending by date")
            if bar.date not in cal_index:
                raise ValueError(f"bar date {bar.date} missing from calendar")
            previous = bar.date

        detections: list[models.ParentDetection] = []
        for bar in ordered:
            index = cal_index[bar.date]
            if index < PLATFORM_WINDOW_DAYS:
                continue
            platform_days = cal[index - PLATFORM_WINDOW_DAYS : index]
            if any(day not in bar_by_date for day in platform_days):
                # The observable platform-breakout parent cannot be established.
                continue
            platform = [bar_by_date[day] for day in platform_days]
            platform_high = max(day.research_high_adj for day in platform)
            platform_low = min(day.research_low_adj for day in platform)
            if not platform_low > 0 or not bar.research_open_adj > 0:
                continue
            if not bar.research_close_adj > platform_high:
                continue
            amplitude = (platform_high - platform_low) / platform_low
            if not amplitude < PLATFORM_AMPLITUDE_THRESHOLD:
                continue

            anchor_date = bar.date
            if index < BOTTOM_WINDOW_DAYS + PLATFORM_WINDOW_DAYS:
                detections.append(
                    _censored(symbol, anchor_date, models.CensorReason.WARMUP_INCOMPLETE)
                )
                continue
            bottom_days = cal[
                index - PLATFORM_WINDOW_DAYS - BOTTOM_WINDOW_DAYS : index - PLATFORM_WINDOW_DAYS
            ]
            if any(day not in bar_by_date for day in bottom_days):
                detections.append(
                    _censored(symbol, anchor_date, models.CensorReason.WARMUP_INCOMPLETE)
                )
                continue
            bottom_window = [bar_by_date[day] for day in bottom_days]
            bottom_low = min(day.research_low_adj for day in bottom_window)
            bottom_high = max(day.research_high_adj for day in bottom_window)
            bottom_range = bottom_high - bottom_low
            if not bottom_range > 0:
                detections.append(
                    _censored(symbol, anchor_date, models.CensorReason.LOW_POSITION_UNDEFINED)
                )
                continue

            reference_close = platform[0].research_close_adj
            bottom_position = (reference_close - bottom_low) / bottom_range
            same_day_gain = bar.research_close_adj / bar.research_open_adj - 1
            prior_mean_volume = sum(day.volume for day in platform) / PLATFORM_WINDOW_DAYS
            volume_expanded = (
                bar.volume >= VOLUME_RATIO_THRESHOLD * prior_mean_volume
                or math.isclose(
                    bar.volume,
                    VOLUME_RATIO_THRESHOLD * prior_mean_volume,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            )
            volume_ratio: float | None = (
                bar.volume / prior_mean_volume if prior_mean_volume > 0 else None
            )

            qualified = (
                (
                    bottom_position <= BOTTOM_POSITION_THRESHOLD
                    or math.isclose(
                        bottom_position,
                        BOTTOM_POSITION_THRESHOLD,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    )
                )
                and (
                    same_day_gain >= SAME_DAY_GAIN_THRESHOLD
                    or math.isclose(
                        same_day_gain,
                        SAME_DAY_GAIN_THRESHOLD,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    )
                )
                and volume_expanded
            )
            evidence = models.DetectionEvidence(
                qualified=qualified,
                values={
                    "bottom_position": bottom_position,
                    "bottom_position_threshold": BOTTOM_POSITION_THRESHOLD,
                    "bottom_window_days": BOTTOM_WINDOW_DAYS,
                    "bottom_position_reference_close_adj": reference_close,
                    "platform_high_adj": platform_high,
                    "platform_low_adj": platform_low,
                    "platform_amplitude": amplitude,
                    "platform_amplitude_threshold": PLATFORM_AMPLITUDE_THRESHOLD,
                    "platform_window_days": PLATFORM_WINDOW_DAYS,
                    "breakout_close_adj": bar.research_close_adj,
                    "breakout_open_adj": bar.research_open_adj,
                    "entity_bottom_adj": min(bar.research_open_adj, bar.research_close_adj),
                    "same_day_gain": same_day_gain,
                    "same_day_gain_threshold": SAME_DAY_GAIN_THRESHOLD,
                    "breakout_volume": bar.volume,
                    "prior_20d_mean_volume": prior_mean_volume,
                    "volume_ratio": volume_ratio,
                    "volume_ratio_threshold": VOLUME_RATIO_THRESHOLD,
                },
            )
            landmark = models.Landmark(
                kind=models.LandmarkKind.SIGNAL_DAY_CLOSE,
                anchor_date=anchor_date,
                landmark_date=anchor_date,
            )
            detections.append(
                models.ParentDetection(
                    factor_id=FACTOR_ID,
                    symbol=symbol,
                    anchor_date=anchor_date,
                    landmark=landmark,
                    evidence=evidence,
                )
            )
        return tuple(detections)


def _censored(
    symbol: str, anchor_date: date, reason: models.CensorReason
) -> models.ParentDetection:
    return models.ParentDetection(
        factor_id=FACTOR_ID,
        symbol=symbol,
        anchor_date=anchor_date,
        landmark=None,
        censor=reason,
    )
