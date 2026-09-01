from datetime import date, timedelta

import pytest

from app.services.daily_event_research.escape_risk import (
    DAILY_SIGNAL_IDS,
    MINUTE_SIGNAL_IDS,
    SIGNAL_CAPABILITIES,
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
    assert all(SIGNAL_CAPABILITIES[item] == "available" for item in MINUTE_SIGNAL_IDS)
    with pytest.raises(ValueError, match="intraday reader required"):
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


def test_oos_adjudication_is_independent_and_fail_closed():
    bars = make_bars([10, 9, 8, 7, 8, 9, 10, 11, 12, 13, 14, 15] * 8)
    detections = [
        event("escape_s8", bars[40].date),
        event("escape_s9", bars[41].date),
    ]
    report = aggregate_escape_signals(
        detections,
        {SYMBOL: bars},
        oos_start=bars[30].date,
    )
    s8 = next(item for item in report.signals if item.signal_id == "s8")
    assert s8.oos is not None
    assert {arm.kind for arm in s8.oos.baselines} == {"no_signal_hold", "ma20", "atr"}
    assert s8.verdict == "unavailable_insufficient_oos_samples"


def test_s10_pit_censor_does_not_affect_other_signals():
    bars = make_bars([10, 9, 8, 7, 8, 9, 10, 11])
    report = aggregate_escape_signals(
        [
            Detection(
                "escape_s10",
                "intraday_v1",
                SYMBOL,
                bars[2].date,
                censor=EscapeCensorReason.PIT_FACT_MISSING,
            ),
            event("escape_s8", bars[2].date),
        ],
        {SYMBOL: bars},
    )
    s10 = next(item for item in report.signals if item.signal_id == "s10")
    s8 = next(item for item in report.signals if item.signal_id == "s8")
    assert EscapeCensorReason.PIT_FACT_MISSING.value in s10.censor_codes
    assert EscapeCensorReason.PIT_FACT_MISSING.value not in s8.censor_codes
    assert all(stat.events == 0 for stat in s10.horizons)
    assert s8.horizons[0].events == 1


def test_low_oos_sample_is_unavailable():
    bars = make_bars([10.0 + index for index in range(50)])
    report = aggregate_escape_signals(
        [event("escape_s8", bars[30].date)],
        {SYMBOL: bars},
        oos_start=bars[20].date,
    )
    signal = next(item for item in report.signals if item.signal_id == "s8")
    assert signal.verdict == "unavailable_insufficient_oos_samples"


def test_minute_gap_stays_censored_without_daily_fallback():
    bars = make_bars([10.0 + index for index in range(20)])
    report = aggregate_escape_signals(
        [],
        {SYMBOL: bars},
        external_censor_codes={"s2": [EscapeCensorReason.INTRADAY_DATA_MISSING.value]},
        oos_start=bars[5].date,
    )
    signal = next(item for item in report.signals if item.signal_id == "s2")
    assert signal.verdict == "unavailable_no_qualified_events"
    assert EscapeCensorReason.INTRADAY_DATA_MISSING.value in signal.censor_codes
    assert all(item.events == 0 for item in signal.horizons)


def test_oos_report_exposes_strongest_baseline_bootstrap_metadata():
    bars = make_bars([10.0 + index for index in range(100)])
    detections = [event("escape_s8", bars[index].date) for index in range(30, 60)]
    report = aggregate_escape_signals(
        detections,
        {SYMBOL: bars},
        oos_start=bars[20].date,
    )
    signal = next(item for item in report.signals if item.signal_id == "s8")
    assert signal.oos is not None
    assert signal.oos.comparator is None or signal.oos.comparator in {
        "no_signal_hold",
        "ma20",
        "atr",
    }
    assert isinstance(signal.oos.valid_replicates, int)


def test_oos_cluster_gate_stays_unavailable_with_one_symbol():
    bars = make_bars([10.0 + index for index in range(100)])
    detections = [event("escape_s8", bars[index].date) for index in range(30, 60)]
    report = aggregate_escape_signals(
        detections,
        {SYMBOL: bars},
        oos_start=bars[20].date,
    )
    signal = next(item for item in report.signals if item.signal_id == "s8")
    assert signal.verdict == "unavailable_insufficient_oos_samples"
    assert signal.oos is not None
    assert signal.oos.valid_replicates == 0
    assert any(value is None for value in signal.oos.bootstrap_lower_by_horizon.values())


def test_ma20_penultimate_close_executes_at_last_bar_open():
    closes = [10.0] * 21 + [1.0, 5.0]
    opens = [10.0] * 22 + [2.0]
    bars = make_bars(closes, opens=opens)
    report = aggregate_escape_signals(
        [event("escape_s9", bars[20].date)],
        {SYMBOL: bars},
        horizons=(3,),
        oos_start=bars[0].date,
    )
    signal = next(item for item in report.signals if item.signal_id == "s9")
    assert signal.oos is not None
    ma20 = next(item for item in signal.oos.baselines if item.kind == "ma20")
    assert ma20.net_return_mean_by_horizon[3] == pytest.approx(2.0 / 10.0 - 1.0 - 0.001)
