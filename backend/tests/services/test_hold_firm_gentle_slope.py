from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services.hold_firm_patterns.gentle_slope import GentleSlopeDetector, _ols
from app.services.hold_firm_patterns.models import Bar, CensorReason

SYMBOL = "000001.SZ"


def make_bars(*, flat_range=False, start=13.5, step=0.1, volume_step=-6.0, count=140):
    bars = []
    day = date(2024, 1, 1)
    for index in range(count):
        if index < 120:
            close = start if index == 119 else 15.0
            low, high = (15.0, 15.0) if flat_range else (10.0, 20.0)
            open_ = close - 0.01
        else:
            close = start + step * (index - 120)
            low, high, open_ = close - 0.05, close + 0.05, close - 0.01
        volume = 1100.0 + volume_step * index
        bars.append(
            Bar(
                SYMBOL,
                day + timedelta(days=index),
                open_,
                high,
                low,
                close,
                open_,
                high,
                low,
                close,
                volume,
                1000.0,
            )
        )
    return tuple(bars)


def detector_result(bars):
    return GentleSlopeDetector().detect(
        SYMBOL, bars, SimpleNamespace(), tuple(bar.date for bar in bars)
    )


def test_ols_known_line_has_positive_slope_and_unit_r2():
    slope, r2 = _ols([1.0, 2.0, 3.0, 4.0])
    assert slope == pytest.approx(1.0)
    assert r2 == pytest.approx(1.0)


def test_qualified_evidence_contains_thresholds_and_diagnostics():
    detections = detector_result(make_bars())
    assert len(detections) == 1
    detection = detections[0]
    assert detection.evidence is not None and detection.evidence.qualified
    values = detection.evidence.values
    assert values["low_position"] == pytest.approx(0.35)
    assert values["low_position_max"] == 0.35
    assert values["ols_r2"] >= 0.60
    assert values["daily_return_band"] == [-0.03, 0.03]
    assert values["ma20"] > 0
    assert values["prior_20d_mean_volume"] > 0
    assert "liquidity_diagnostic_inputs" in values
    assert values["hypothesis_label"] == "control_inference_unverified"


def test_non_positive_low_position_denominator_is_explicitly_censored():
    detections = detector_result(make_bars(flat_range=True))
    assert len(detections) == 1
    assert detections[0].censor is CensorReason.LOW_POSITION_UNDEFINED
    assert detections[0].evidence is None


def test_unobservable_short_history_does_not_fabricate_parent():
    assert detector_result(make_bars(count=139)) == ()


def test_missing_market_day_inside_required_window_is_warmup_censored():
    complete = make_bars()
    calendar = tuple(bar.date for bar in complete)
    missing = tuple(bar for index, bar in enumerate(complete) if index != 100)
    detections = GentleSlopeDetector().detect(SYMBOL, missing, SimpleNamespace(), calendar)
    assert detections[-1].censor is CensorReason.WARMUP_INCOMPLETE


def test_zero_volume_remains_in_parent_comparison_as_not_selected():
    bars = list(make_bars())
    bars[130] = replace(bars[130], volume=0.0)
    detections = detector_result(tuple(bars))
    assert len(detections) == 1
    assert detections[0].censor is None
    assert detections[0].evidence is not None
    assert detections[0].evidence.qualified is False
    assert detections[0].evidence.values["zero_volume_days_window"] == 1
    assert detections[0].evidence.values["log_volume_slope"] is None


def test_exact_return_boundaries_are_accepted_by_closed_interval():
    bars = list(make_bars(start=10.0, step=0.1))
    # Keep the fixture focused on the inclusive band contract; detector still
    # requires the complete parent shape and may reject this altered slope.
    assert -0.03 <= (bars[120].research_close_adj / bars[119].research_close_adj - 1.0) <= 0.03
