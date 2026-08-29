from datetime import date, timedelta

import pytest

from app.data_providers.fquant.daily_market_research import MarketFact
from app.services.daily_event_research.models import Detection, DetectionEvidence
from app.services.daily_event_research.pre_surge import (
    PreSurgeCensorReason,
    PreSurgeParams,
    PreSurgeStudyAggregator,
    PreSurgeVerdict,
    VARIANT_F1,
    VARIANT_F2,
    VARIANT_F3,
    VARIANT_F4,
    detect_f1_limit_up,
    detect_f2_gap_unfilled,
    detect_f3_relative_bullish,
    detect_f4_volume_stack,
    detection_payload,
)
from app.services.hold_firm_patterns.models import Bar

SYMBOL = "000001.SZ"
START = date(2024, 1, 1)


def bar(index, *, close=10.0, open_=None, low=None, volume=100.0):
    day = START + timedelta(days=index)
    open_value = close if open_ is None else open_
    low_value = min(close, open_value) if low is None else low
    high = max(close, open_value) + 0.1
    return Bar(
        SYMBOL,
        day,
        open_value,
        high,
        low_value,
        close,
        open_value,
        high,
        low_value,
        close,
        volume,
        volume * close,
    )


def fact(*, pre_close=10.0, limit_up=None, regime="main_10", is_st=False):
    return MarketFact(
        raw_open=pre_close,
        raw_high=limit_up or pre_close,
        raw_low=pre_close,
        raw_close=limit_up or pre_close,
        pre_close=pre_close,
        published_limit_up=limit_up,
        published_limit_down=pre_close * 0.9,
        regime=regime,
        is_st=is_st,
        name="测试",
    )


@pytest.mark.parametrize(
    ("regime", "limit_close"),
    (("main_10", 11.0), ("chinext_20", 12.0), ("beijing_30", 13.0)),
)
def test_f1_uses_pit_limit_regime(regime, limit_close):
    bars = (bar(0, close=limit_close),)
    facts = {(SYMBOL, bars[0].date): fact(regime=regime)}
    result = detect_f1_limit_up(
        SYMBOL,
        bars,
        facts,
        (bars[0].date,),
        PreSurgeParams(f1_lookback_days=1),
    )
    assert result[0].evidence is not None
    assert result[0].evidence.qualified is True


def test_f1_st_override_is_five_percent_when_published_ztj_missing():
    bars = (bar(0, close=10.5),)
    facts = {(SYMBOL, bars[0].date): fact(regime="main_10", is_st=True)}
    result = detect_f1_limit_up(
        SYMBOL,
        bars,
        facts,
        (bars[0].date,),
        PreSurgeParams(f1_lookback_days=1),
    )
    assert result[0].evidence is not None
    assert result[0].evidence.values["limit_sources"] == {"pit_is_st:5pct": 1}


def test_f2_is_only_emitted_on_third_complete_confirmation_day():
    bars = (
        bar(0, close=10.0),
        bar(1, close=10.2, open_=10.3, low=10.1),
        bar(2, close=10.3, low=10.05),
        bar(3, close=10.4, low=10.02),
        bar(4, close=10.5, low=10.01),
    )
    calendar = tuple(item.date for item in bars)
    full = detect_f2_gap_unfilled(SYMBOL, bars, calendar)
    matched = [item for item in full if item.evidence and item.evidence.qualified]
    assert len(matched) == 1
    assert matched[0].signal_date == bars[4].date
    assert matched[0].evidence.values["gap_date"] == bars[1].date.isoformat()
    assert matched[0].evidence.values["available_date"] == bars[4].date.isoformat()
    truncated = detect_f2_gap_unfilled(SYMBOL, bars[:-1], calendar[:-1])
    assert not any(item.evidence and item.evidence.qualified for item in truncated)


def test_f3_missing_benchmark_is_explicitly_censored():
    bars = tuple(bar(index, close=10.0 + index) for index in range(4))
    result = detect_f3_relative_bullish(
        SYMBOL,
        bars,
        None,
        tuple(item.date for item in bars),
        PreSurgeParams(f3_min_streak=2, f3_window_days=3),
    )
    assert result
    assert all(item.censor is PreSurgeCensorReason.MISSING_BENCHMARK for item in result)


def test_f4_requires_warmup_then_detects_sustained_volume_stack():
    volumes = [100, 100, 100, 300, 400]
    bars = tuple(bar(index, volume=value) for index, value in enumerate(volumes))
    result = detect_f4_volume_stack(
        SYMBOL,
        bars,
        tuple(item.date for item in bars),
        PreSurgeParams(f4_baseline_days=3, f4_stack_days=2),
    )
    assert any(item.censor is PreSurgeCensorReason.WARMUP_INSUFFICIENT for item in result)
    assert result[-1].evidence is not None
    assert result[-1].evidence.qualified is True


def detection(variant, qualified):
    return Detection(
        detector_id="pre_surge",
        variant=variant,
        symbol=SYMBOL,
        signal_date=START,
        evidence=DetectionEvidence(
            qualified=qualified,
            values={"available_date": START.isoformat()},
        ),
    )


def test_necessary_and_sufficient_denominators_and_verdicts_are_independent():
    aggregator = PreSurgeStudyAggregator()
    for index in range(100):
        qualified = index < 40
        future_surge = index < 30 or 40 <= index < 60
        aggregator.record(detection(VARIANT_F1, qualified), future_surge)
    for index in range(100):
        qualified = index < 40
        future_surge = index < 20 or 40 <= index < 60
        aggregator.record(detection(VARIANT_F2, qualified), future_surge)
    for index in range(20):
        aggregator.record(detection(VARIANT_F3, True), index < 10)

    stats = aggregator.summarize()
    assert stats[VARIANT_F1].necessary_rate == pytest.approx(0.6)
    assert stats[VARIANT_F1].sufficient_rate == pytest.approx(0.75)
    assert stats[VARIANT_F1].baseline_rate == pytest.approx(0.5)
    assert stats[VARIANT_F1].verdict is PreSurgeVerdict.ACCEPTED
    assert stats[VARIANT_F2].verdict is PreSurgeVerdict.REJECTED
    assert stats[VARIANT_F3].verdict is PreSurgeVerdict.UNAVAILABLE


def test_detection_payload_is_json_safe():
    payload = detection_payload(detection(VARIANT_F4, True))
    assert payload["signal_date"] == START.isoformat()
    assert payload["evidence"]["qualified"] is True
