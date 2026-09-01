"""Pure, separately reported F2 entry predicates."""

from __future__ import annotations

from collections.abc import Sequence
from statistics import mean

from .models import (
    FLAG_RETEST_TOLERANCE,
    FLAG_VOLUME_SHRINK_RATIO,
    RESTART_VOLUME_RATIO,
    WEEKLY_ZIGZAG_THRESHOLD,
)
from .weekly import WeeklyBar


def weekly_reclaim(previous: WeeklyBar, current: WeeklyBar, pole_high: float) -> bool:
    previous_body_is_real = previous.close <= previous.open * (1.0 - WEEKLY_ZIGZAG_THRESHOLD)
    return (
        previous_body_is_real
        and previous.close < previous.open
        and current.close > current.open
        and current.open <= previous.close
        and current.close >= previous.open
        and current.close < pole_high
    )


def volume_shrink_restart(
    pole: Sequence[WeeklyBar], flag: Sequence[WeeklyBar], current: WeeklyBar
) -> bool:
    if not pole or not flag:
        return False
    pole_volume = mean(bar.volume for bar in pole)
    flag_volume = mean(bar.volume for bar in flag)
    return (
        flag_volume <= pole_volume * FLAG_VOLUME_SHRINK_RATIO
        and current.volume >= flag_volume * RESTART_VOLUME_RATIO
        and current.close > current.open
        and current.close > max(bar.close for bar in flag)
    )


def flag_low_retest(current: WeeklyBar, flag_low: float) -> bool:
    return current.low <= flag_low * (1.0 + FLAG_RETEST_TOLERANCE) and current.close > flag_low


__all__ = ["flag_low_retest", "volume_shrink_restart", "weekly_reclaim"]
