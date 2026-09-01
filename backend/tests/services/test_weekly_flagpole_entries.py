from datetime import date

from app.services.weekly_flagpole.entries import (
    flag_low_retest,
    volume_shrink_restart,
    weekly_reclaim,
)
from app.services.weekly_flagpole.weekly import WeeklyBar


def b(o, h, low, c, v):
    return WeeklyBar(
        "x", date(2026, 1, 2), date(2026, 1, 2), date(2026, 1, 2), o, h, low, c, v, 5, True
    )


def test_reclaim_is_body_engulfing_and_flag_internal():
    assert weekly_reclaim(b(12, 12.5, 11, 11, 100), b(10.8, 13, 10.5, 12.5, 100), 14)
    assert not weekly_reclaim(b(12, 12.5, 11, 11.5, 100), b(10.8, 13, 10.5, 12.5, 100), 14)


def test_volume_restart_and_retest_boundaries():
    pole = [b(10, 11, 9.8, 10.8, 100), b(10.8, 12, 10.5, 11.8, 100)]
    flag = [b(11.8, 12, 11, 11.5, 70)]
    assert volume_shrink_restart(pole, flag, b(11.5, 13, 11.4, 12.5, 84))
    assert flag_low_retest(b(11, 11.5, 10.2, 11.1, 80), 10)
    assert not flag_low_retest(b(11, 11.5, 10.21, 11.1, 80), 10)
