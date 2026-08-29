from datetime import date, timedelta

import pytest

from app.services.daily_event_research.escape_risk import (
    DAILY_SIGNAL_IDS,
    MINUTE_SIGNAL_IDS,
    SIGNAL_CAPABILITIES,
    SIGNAL_CAPABILITY_MINUTE_UNAVAILABLE,
    EscapeCensorReason,
    EscapeS1Detector,
    EscapeS8Detector,
    EscapeS9Detector,
    aggregate_escape_signals,
    capability_for,
    macd_histogram,
    require_daily_signal,
)
from app.services.daily_event_research.models import CensorReason, Detection, DetectionEvidence
from app.services.hold_firm_patterns.models import Bar

SYMBOL = "000001.SZ"
START = date(2024, 1, 1)


def make_bars(closes, opens=None):
    opens = list(opens or closes)
    bars = []
    for index, close in enumerate(closes):
        opening = opens[index]
        bars.append(
            Bar(
                SYMBOL,
                START + timedelta(days=index),
                opening,
                max(opening, close) + 0.01,
                min(opening, close) - 0.01,
                close,
                opening,
                max(opening, close) + 0.01,
                min(opening, close) - 0.01,
                close,
                100.0,
                1000.0,
            )
        )
    return tuple(bars)


def run(detector, bars):
    return detector.detect(SYMBOL, bars, tuple(bar.date for bar in bars))


def test_macd_red_run_adjacent_peaks_are_strictly_declining():
    from app.services.daily_event_research.escape_risk import _red_runs

    runs = _red_runs((-1, 1, 3, -1, 1, 2, -1), 0)
    assert [(run.start, run.end, run.peak) for run in runs] == [(1, 2, 3), (4, 5, 2)]
    assert runs[1].peak < runs[0].peak
    assert macd_histogram([10.0, 11.0, 10.0])[-1] == pytest.approx(
        macd_histogram([10.0, 11.0, 10.0])[-1]
    )


def test_s1_warmup_censor_and_prefix_invariance():
    bars = make_bars([10.0 + index for index in range(80)])
    detector = EscapeS1Detector()
    full = run(detector, bars)
    cut = bars[:70]
    truncated = run(detector, cut)
    assert tuple(item for item in full if item.signal_date <= cut[-1].date) == truncated
    assert any(item.censor is CensorReason.WARMUP_INCOMPLETE for item in full)


def test_s8_three_yin_and_doji_breaks_streak():
    bars = make_bars([9, 9, 8, 7, 8], opens=[10, 10, 10, 10, 7])
    detections = run(EscapeS8Detector(), bars)
    qualified = [item for item in detections if item.evidence and item.evidence.qualified]
    assert [item.signal_date for item in qualified] == [bars[2].date, bars[3].date]
    assert not any(
        item.evidence and item.evidence.qualified
        for item in detections
        if item.signal_date == bars[4].date
    )
    assert run(EscapeS8Detector(), bars[:2])[-1].censor is CensorReason.WARMUP_INCOMPLETE


def test_s9_low_open_boundary_and_position_eligibility():
    bars = make_bars([100, 95, 95.01], opens=[100, 95, 95.01])
    detections = run(EscapeS9Detector(), bars)
    assert detections[0].censor is EscapeCensorReason.PIT_FACT_MISSING
    assert detections[1].evidence and detections[1].evidence.qualified
    assert detections[1].evidence.values["available_date"] == bars[1].date.isoformat()
    assert detections[1].evidence.values["available_at_session"] == "open"
    assert detections[1].evidence.values["existing_position_required"] is True
    assert detections[2].evidence and not detections[2].evidence.qualified


def test_s8_prefix_invariance_and_non_position_signal():
    bars = make_bars([10, 9, 8, 7, 6], opens=[10, 10, 10, 10, 10])
    full = run(EscapeS8Detector(), bars)
    cut = bars[:4]
    assert tuple(item for item in full if item.signal_date <= cut[-1].date) == run(
        EscapeS8Detector(), cut
    )
    assert full[-1].evidence.values["existing_position_required"] is False


def event(detector_id, signal_date, qualified=True):
    return Detection(
        detector_id,
        "daily_v1",
        SYMBOL,
        signal_date,
        evidence=DetectionEvidence(qualified, {"available_date": signal_date.isoformat()}),
    )


def test_aggregation_horizons_cost_sell_fly_and_avoidance_are_symmetric():
    closes = [100, 100, 94, 104, 103, 94, 85, 84, 83, 82, 81, 80]
    bars = make_bars(closes, opens=[100, 100, 90, 104, 103, 100, 94, 85, 84, 83, 82, 81])
    detections = [event("escape_s9", bars[2].date), event("escape_s9", bars[5].date)]
    report = aggregate_escape_signals(detections, {SYMBOL: bars}, cost_bps=10)
    signal = next(item for item in report.signals if item.signal_id == "s9")
    assert report.horizons == (1, 3, 5, 10)
    one = signal.horizons[0]
    assert one.events == 2
    assert one.rise_events == 1 and one.fall_events == 1
    assert one.missed_escape_rate == pytest.approx(0.5)
    assert one.missed_gain_mean is not None and one.avoidance_depth_mean is not None
    assert one.net_forward_return_mean == pytest.approx(
        one.forward_return_mean - report.round_trip_cost
    )
    assert signal.horizons[-1].horizon_incomplete_events == 1
    assert {item.kind: item.status for item in signal.baselines}[
        "ma20"
    ] == "unavailable_no_baseline"


def test_per_signal_verdicts_and_count_only_grouping():
    bars = make_bars([10, 9, 8, 7, 8, 9, 10])
    detections = [event("escape_s8", bars[2].date), event("escape_s9", bars[2].date)]
    report = aggregate_escape_signals(detections, {SYMBOL: bars})
    verdicts = {item.signal_id: item.verdict for item in report.signals}
    assert verdicts["s8"] == "unavailable_no_frozen_oos_baseline"
    assert verdicts["s9"] == "unavailable_no_frozen_oos_baseline"
    assert report.count_buckets[0].signal_count == 2
    assert report.count_buckets[0].events == 1


def test_capability_fail_closed_for_minute_signals_and_approximation():
    assert set(DAILY_SIGNAL_IDS) == {"s1", "s8", "s9"}
    assert set(MINUTE_SIGNAL_IDS) == {"s2", "s3", "s4", "s5", "s6", "s7", "s10"}
    assert all(
        SIGNAL_CAPABILITIES[item] == SIGNAL_CAPABILITY_MINUTE_UNAVAILABLE
        for item in MINUTE_SIGNAL_IDS
    )
    with pytest.raises(ValueError, match="unavailable_insufficient_immutable_history"):
        require_daily_signal("s3")
    with pytest.raises(ValueError, match="approximation"):
        aggregate_escape_signals([], {}, minute_approximation=True)
    assert capability_for("s1") == "available"


def test_benchmark_requirement_is_explicitly_censored():
    bars = make_bars([10, 9, 8, 7])
    report = aggregate_escape_signals(
        [event("escape_s8", bars[0].date)], {SYMBOL: bars}, require_benchmark=True
    )
    signal = next(item for item in report.signals if item.signal_id == "s8")
    assert signal.verdict == "unavailable_benchmark_missing"
    assert EscapeCensorReason.BENCHMARK_MISSING.value in signal.censor_codes
