from datetime import date

import pytest

from app.services.doji_patterns.models import VolumeState
from app.services.doji_patterns.morphology import (
    candle_metrics,
    is_doji,
    is_gravestone,
    is_t_bar,
    position_percentile,
    prior_move_pct,
    volume_state,
)
from app.services.hold_firm_patterns.models import Bar


def bar(o=10.0, h=11.0, low=9.0, c=10.0, v=100.0):
    return Bar("600000.SH", date(2026, 1, 1), o, h, low, c, o, h, low, c, v, v * c)


def test_doji_theta_boundary_and_zero_range():
    assert is_doji(bar(10, 11, 9, 10.2), theta=0.1)
    assert not is_doji(bar(10, 11, 9, 10.21), theta=0.1)
    assert candle_metrics(bar(10, 10, 10, 10)).body_ratio is None
    assert not is_doji(bar(10, 10, 10, 10))


def test_shadow_boundaries_are_inclusive_and_mirrored():
    assert is_gravestone(bar(10, 12, 9.9, 10.1), theta=0.1)
    assert is_t_bar(bar(10, 10.1, 8, 10.1), theta=0.1)


def test_position_prior_move_and_volume_edges():
    w = [bar(10, 11, 9, 10) for _ in range(19)] + [bar(10, 12, 8, 11)]
    assert position_percentile(w) == pytest.approx(0.75)
    assert position_percentile([bar(10, 10, 10, 10)]) is None
    assert prior_move_pct([bar(c=10), bar(c=11)]) == pytest.approx(0.1)
    assert volume_state(70, 100) is VolumeState.SHRINK
    assert volume_state(150, 100) is VolumeState.EXPAND
    assert volume_state(100, 100) is VolumeState.FLAT
    assert volume_state(1, 0) is None
