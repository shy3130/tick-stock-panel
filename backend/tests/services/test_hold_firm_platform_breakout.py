from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.hold_firm_patterns.models import Bar, CensorReason, FACTOR_IDS, LandmarkKind
from app.services.hold_firm_patterns.platform_breakout import PlatformBreakoutDetector

SYMBOL = "600000.SH"


def _bar(
    day: date, *, open_: float, high: float, low: float, close: float, volume: float = 1000.0
) -> Bar:
    return Bar(
        symbol=SYMBOL,
        date=day,
        research_open_adj=open_,
        research_high_adj=high,
        research_low_adj=low,
        research_close_adj=close,
        quote_open_raw=open_,
        quote_high_raw=high,
        quote_low_raw=low,
        quote_close_raw=close,
        volume=volume,
        amount=volume * close,
    )


def _series(
    *,
    bottom_days: int = 120,
    bottom_low: float = 9.5,
    bottom_high: float = 15.0,
    bottom_close: float = 10.0,
    platform_high: float = 11.2,
    platform_low: float = 10.8,
    breakout_open: float = 11.25,
    breakout_close: float = 11.9,
    breakout_volume: float = 2000.0,
) -> list[Bar]:
    start = date(2024, 1, 2)
    bars = [
        _bar(
            start + timedelta(days=index),
            open_=bottom_close,
            high=bottom_high,
            low=bottom_low,
            close=bottom_close,
        )
        for index in range(bottom_days)
    ]
    platform_start = start + timedelta(days=bottom_days)
    bars.extend(
        _bar(
            platform_start + timedelta(days=index),
            open_=(platform_high + platform_low) / 2,
            high=platform_high,
            low=platform_low,
            close=(platform_high + platform_low) / 2,
        )
        for index in range(20)
    )
    breakout_date = platform_start + timedelta(days=20)
    bars.append(
        _bar(
            breakout_date,
            open_=breakout_open,
            high=max(breakout_open, breakout_close),
            low=min(breakout_open, breakout_close),
            close=breakout_close,
            volume=breakout_volume,
        )
    )
    return bars


class _NoFacts:
    def row(self, symbol: str, day: date):
        raise AssertionError("F4 detector must not read market facts")


def _detect(bars: list[Bar]):
    return PlatformBreakoutDetector().detect(
        SYMBOL, bars, _NoFacts(), tuple(bar.date for bar in bars)
    )


def test_qualified_breakout_contains_selection_and_diagnostic_anchors() -> None:
    detections = _detect(_series())
    assert len(detections) == 1
    detection = detections[0]
    assert detection.factor_id == FACTOR_IDS[3] == "bottom_platform_breakout"
    assert detection.censor is None
    assert detection.landmark is not None
    assert detection.landmark.kind is LandmarkKind.SIGNAL_DAY_CLOSE
    assert detection.evidence is not None and detection.evidence.qualified
    values = detection.evidence.values
    assert values["platform_high_adj"] == pytest.approx(11.2)
    assert values["entity_bottom_adj"] == pytest.approx(11.25)
    assert values["breakout_close_adj"] == pytest.approx(11.9)
    assert values["bottom_position_threshold"] == 0.35
    assert values["bottom_position_reference_close_adj"] == pytest.approx(11.0)
    assert values["platform_amplitude_threshold"] == 0.15
    assert values["same_day_gain_threshold"] == 0.05
    assert values["volume_ratio_threshold"] == 1.5
    assert values["same_day_gain"] >= 0.05
    assert values["volume_ratio"] == pytest.approx(2.0)


def test_breakout_without_big_yang_is_not_selected() -> None:
    detections = _detect(_series(breakout_close=11.4))
    assert len(detections) == 1
    assert detections[0].evidence is not None
    assert not detections[0].evidence.qualified


def test_breakout_without_volume_expansion_is_not_selected() -> None:
    detections = _detect(_series(breakout_volume=1400.0))
    assert len(detections) == 1
    assert detections[0].evidence is not None
    assert not detections[0].evidence.qualified


def test_bottom_position_above_threshold_is_not_selected() -> None:
    detections = _detect(_series(bottom_high=11.0))
    assert len(detections) == 1
    assert detections[0].evidence is not None
    assert detections[0].evidence.values["bottom_position"] > 0.35
    assert not detections[0].evidence.qualified


def test_threshold_boundaries_are_inclusive() -> None:
    bars = _series(
        bottom_low=8.0,
        bottom_high=8.0 + 3.0 / 0.35,
        breakout_close=11.8125,
        breakout_volume=1500.0,
    )
    detections = _detect(bars)
    assert len(detections) == 1
    assert detections[0].evidence is not None and detections[0].evidence.qualified
    assert detections[0].evidence.values["bottom_position"] == pytest.approx(0.35)
    assert detections[0].evidence.values["same_day_gain"] == pytest.approx(0.05)
    assert detections[0].evidence.values["volume_ratio"] == pytest.approx(1.5)


def test_wide_platform_and_non_strict_close_do_not_create_parent_events() -> None:
    assert _detect(_series(platform_high=13.0, breakout_open=13.1, breakout_close=13.8)) == ()
    assert _detect(_series(breakout_close=11.2)) == ()


def test_incomplete_bottom_warmup_is_censored() -> None:
    detections = _detect(_series(bottom_days=9))
    assert len(detections) == 1
    detection = detections[0]
    assert detection.evidence is None
    assert detection.landmark is None
    assert detection.censor is CensorReason.WARMUP_INCOMPLETE


def test_missing_market_day_inside_platform_does_not_fabricate_parent() -> None:
    complete = _series()
    calendar = tuple(bar.date for bar in complete)
    missing = [bar for index, bar in enumerate(complete) if index != 125]
    assert PlatformBreakoutDetector().detect(SYMBOL, missing, _NoFacts(), calendar) == ()


def test_zero_bottom_range_is_low_position_censor() -> None:
    detections = _detect(_series(bottom_low=10.0, bottom_high=10.0, bottom_close=10.0))
    assert detections
    assert all(detection.evidence is None for detection in detections)
    assert any(detection.censor is CensorReason.LOW_POSITION_UNDEFINED for detection in detections)


def test_future_five_days_cannot_change_detection() -> None:
    bars = _series()
    before = _detect(bars)
    future_start = bars[-1].date + timedelta(days=1)
    future = [
        _bar(
            future_start + timedelta(days=index),
            open_=50.0,
            high=51.0,
            low=4.0,
            close=5.0,
            volume=1.0,
        )
        for index in range(5)
    ]
    assert _detect(bars + future) == before
