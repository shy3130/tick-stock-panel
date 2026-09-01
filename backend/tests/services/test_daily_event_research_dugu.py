from datetime import date, timedelta

import pytest

from app.services.daily_event_research.dugu_trend import (
    DUGU_ALIGNMENT_DAY_CHOICES,
    DUGU_ALIGNMENT_DAYS_DEFAULT,
    DUGU_BAND_MODES,
    DUGU_REQUIRE_M3_CHOICES,
    DUGU_VARIANTS,
    DuguTrendConfig,
    DuguTrendDetector,
    DUGU_SCAN_AXES,
    dugu_scan_cell_id,
    iter_dugu_scan_grid,
    resolve_dugu_config,
)
from app.services.hold_firm_patterns.models import Bar
from app.services.daily_event_research.models import CensorReason

SYMBOL = "000001.SZ"


def make_bars(count=260, dip_at=220, dip=0.35):
    start = date(2024, 1, 1)
    closes = [10.0 + 0.05 * i for i in range(count)]
    if dip_at < count - 1:
        closes[dip_at] -= dip
        closes[dip_at + 1] -= dip * 0.5
    bars = []
    for i, close in enumerate(closes):
        low = close - (dip if i == dip_at else 0.01)
        bars.append(
            Bar(
                SYMBOL,
                start + timedelta(days=i),
                close,
                close + 0.02,
                low,
                close,
                close,
                close + 0.02,
                low,
                close,
                100.0,
                1000.0,
            )
        )
    return tuple(bars)


def run(bars, **kwargs):
    detector = DuguTrendDetector(DuguTrendConfig(**kwargs))
    return detector.detect(SYMBOL, bars, tuple(bar.date for bar in bars))


def test_truncation_is_prefix_invariant():
    bars = make_bars()
    full = run(bars, require_m3=True)
    cut = bars[:240]
    truncated = run(cut, require_m3=True)
    assert tuple(item for item in full if item.signal_date <= cut[-1].date) == truncated


def test_ma200_warmup_is_explicitly_censored_when_trigger_exists():
    detections = run(make_bars(count=150, dip_at=120), require_m3=False)
    assert detections
    assert any(item.censor is CensorReason.WARMUP_INCOMPLETE for item in detections)


def test_t1_t2_t3_evidence_is_recorded():
    detections = run(make_bars())
    evidenced = [item for item in detections if item.evidence is not None]
    assert evidenced
    values = evidenced[-1].evidence.values
    assert values["t1"] is True
    assert values["t2"] is True
    assert values["t3"] is True


def test_m3_switch_uses_frozen_twenty_day_return_cap_without_lookahead():
    bars = make_bars()
    on = run(bars, require_m3=True)
    evidenced = [item for item in on if item.evidence is not None]
    assert evidenced
    for item in evidenced:
        values = item.evidence.values
        assert values["m3_required"] is True
        assert values["m3_max_return"] == 0.30
        assert values["m3_pass"] is (values["m3_return_20d"] <= 0.30)


def test_fixed_and_atr_bands_are_distinct_registered_modes():
    fixed = run(make_bars(), band_mode="fixed")
    atr = run(make_bars(), band_mode="atr")
    assert fixed or atr
    if fixed and atr:
        assert {item.evidence.values["band_mode"] for item in fixed if item.evidence} <= {"fixed"}
        assert {item.evidence.values["band_mode"] for item in atr if item.evidence} <= {"atr"}


def test_missing_calendar_day_is_not_filled():
    bars = make_bars()
    missing = bars[:219] + bars[220:]
    detections = run(missing)
    assert all(item.signal_date != bars[220].date for item in detections)
def test_alignment_scan_grid_is_frozen_and_complete():
    assert DUGU_ALIGNMENT_DAY_CHOICES == (10, 30, 50, 100)
    assert DUGU_ALIGNMENT_DAYS_DEFAULT == 30
    assert DUGU_SCAN_AXES["variant"] == tuple(DUGU_VARIANTS)
    assert DUGU_SCAN_AXES["band_mode"] == DUGU_BAND_MODES
    assert DUGU_SCAN_AXES["require_m3"] == DUGU_REQUIRE_M3_CHOICES

    grid = iter_dugu_scan_grid()
    expected_count = (
        len(DUGU_ALIGNMENT_DAY_CHOICES)
        * len(DUGU_VARIANTS)
        * len(DUGU_BAND_MODES)
        * len(DUGU_REQUIRE_M3_CHOICES)
    )
    assert len(grid) == expected_count
    cells = {dugu_scan_cell_id(config) for config in grid}
    assert len(cells) == expected_count
    combinations = {
        (config.alignment_days, config.variant, config.band_mode, config.require_m3)
        for config in grid
    }
    assert len(combinations) == expected_count


def test_alignment_days_are_grid_validated_and_disclosed():
    with pytest.raises(ValueError, match="frozen scan"):
        resolve_dugu_config(alignment_days=25)

    detections = run(make_bars(), alignment_days=10)
    evidenced = [item for item in detections if item.evidence is not None]
    assert evidenced
    for item in evidenced:
        values = item.evidence.values
        assert values["alignment_days"] == 10
        assert values["alignment_hold_days"] >= 10
        assert values["t1"] is (values["close"] > values["ma_fast"])
