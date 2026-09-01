from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.data_providers.fquant.daily_market_research import IntradayFloatSharesFact
from app.data_providers.fquant.escape_risk_intraday import (
    EscapeRiskIntradayBundle,
    IntradayDay,
    IntradayMinute,
)
from app.services.daily_event_research.escape_risk import aggregate_escape_signals
from app.services.daily_event_research.escape_risk_intraday import (
    detect_intraday_escape_signals,
)
from app.services.daily_event_research.models import Detection, DetectionEvidence
from app.services.hold_firm_patterns.models import Bar

SYMBOL = "000001.SZ"
CST = ZoneInfo("Asia/Shanghai")


def _timestamp(day, index):
    if index <= 119:
        clock = datetime.combine(day, time(9, 31), tzinfo=CST) + timedelta(minutes=index)
    else:
        clock = datetime.combine(day, time(13, 1), tzinfo=CST) + timedelta(minutes=index - 120)
    return clock


def make_day(
    day,
    *,
    prices=None,
    highs=None,
    lows=None,
    volumes=None,
    vwaps=None,
    open_price=100.0,
    pre_close=100.0,
    limit_up=110.0,
    limit_down=90.0,
    available_at=None,
    source_day=None,
):
    prices = list(prices or [100.0] * 240)
    highs = list(highs or prices)
    lows = list(lows or prices)
    volumes = list(volumes or [100] * 240)
    vwaps = list(vwaps or prices)
    minutes = tuple(
        IntradayMinute(
            index,
            _timestamp(day, index),
            prices[index],
            highs[index],
            lows[index],
            volumes[index],
            prices[index] * volumes[index],
            vwaps[index],
        )
        for index in range(240)
    )
    source_day = source_day or day - timedelta(days=1)
    turnover = (
        None
        if available_at is None
        else IntradayFloatSharesFact(
            float_shares=1_000_000,
            available_at=available_at,
            source_day=source_day,
        )
    )
    return IntradayDay(
        SYMBOL,
        day,
        minutes,
        open_price,
        pre_close,
        limit_up,
        limit_down,
        turnover,
    )


def evaluate(current, history=None):
    history = list(history or [])
    calendar = tuple(item.trade_date for item in [*history, current])
    rows = {(item.symbol, item.trade_date): item for item in [*history, current]}
    return detect_intraday_escape_signals(
        EscapeRiskIntradayBundle(rows, {}),
        symbols=[SYMBOL],
        calendar=calendar,
        start=current.trade_date,
        end=current.trade_date,
    )


def evidence_by_signal(result):
    return {
        detection.detector_id.removeprefix("escape_"): detection.evidence
        for detection in result.detections
    }


def history_before(day, *, volume=100, available=True):
    rows = []
    for offset in range(5, 0, -1):
        value = day - timedelta(days=offset)
        rows.append(
            make_day(
                value,
                volumes=[volume] * 240,
                available_at=(
                    datetime.combine(value - timedelta(days=1), time(15), tzinfo=CST)
                    if available
                    else None
                ),
            )
        )
    return rows


def test_s2_tail_dive_and_s3_opened_limit_up_are_deterministic():
    day = date(2025, 8, 28)
    prices = [100.0] * 211 + [100.0 - 3.0 * (index - 210) / 29 for index in range(211, 240)]
    highs = list(prices)
    highs[50:53] = [110.0, 110.0, 110.0]
    result = evaluate(make_day(day, prices=prices, highs=highs), history_before(day))
    evidence = evidence_by_signal(result)
    assert evidence["s2"].qualified is True
    assert evidence["s2"].values["execution_session"] == "next_open"
    assert evidence["s2"].values["tail_start"].endswith("14:30:00+08:00")
    assert evidence["s3"].qualified is True
    assert evidence["s3"].values["sealed_at_close"] is False
    assert evidence["s3"].values["open_count"] == 1


def test_s3_counts_opening_episodes_after_reseal():
    day = date(2025, 8, 28)
    prices = [100.0] * 240
    highs = list(prices)
    highs[50:53] = [110.0, 110.0, 110.0]
    prices[60] = highs[60] = 110.0
    highs[61] = 110.0
    evidence = evidence_by_signal(
        evaluate(
            make_day(day, prices=prices, highs=highs),
            history_before(day),
        )
    )["s3"]
    assert evidence.qualified is True
    assert evidence.values["open_count"] == 2


def test_s4_s5_s6_s7_frozen_thresholds():
    day = date(2025, 8, 28)
    prices = [101.0] * 240
    highs = [101.0] * 240
    lows = [101.0] * 240
    highs[10] = 105.0
    lows[100] = 90.0
    vwaps = [102.0] * 240
    current = make_day(
        day,
        prices=prices,
        highs=highs,
        lows=lows,
        volumes=[300] * 240,
        vwaps=vwaps,
        open_price=103.0,
    )
    result = evaluate(current, history_before(day, volume=100))
    evidence = evidence_by_signal(result)
    assert evidence["s4"].qualified is True
    assert evidence["s5"].qualified is True
    assert evidence["s5"].values["branch"] == "reopened_above_pre_close"
    assert evidence["s6"].qualified is True
    assert evidence["s7"].qualified is True


def test_s7_accepts_flat_or_declining_first_window():
    day = date(2025, 8, 28)
    prices = [100.0 - index / 100 for index in range(240)]
    evidence = evidence_by_signal(
        evaluate(
            make_day(
                day,
                prices=prices,
                highs=prices,
                vwaps=[101.0] * 240,
                open_price=100.0,
            ),
            history_before(day),
        )
    )["s7"]
    assert evidence.qualified is True
    assert evidence.values["available_at"].endswith("10:30:00+08:00")


def test_s5_execution_waits_until_limit_down_reopens():
    day = date(2025, 8, 28)
    prices = [100.0] * 240
    lows = [100.0] * 240
    prices[80] = lows[80] = 90.0
    prices[81] = lows[81] = 90.0
    prices[82] = lows[82] = 91.0
    result = evaluate(
        make_day(day, prices=prices, lows=lows),
        history_before(day),
    )
    evidence = evidence_by_signal(result)["s5"]
    assert evidence.qualified is True
    assert evidence.values["branch"] == "reopened_below_pre_close"
    assert evidence.values["execution_price"] == 91.0
    assert evidence.values["execution_reachable"] is True


def test_s5_never_reopened_is_not_marked_reachable():
    day = date(2025, 8, 28)
    prices = [100.0] * 100 + [90.0] * 140
    lows = list(prices)
    evidence = evidence_by_signal(
        evaluate(
            make_day(day, prices=prices, lows=lows),
            history_before(day),
        )
    )["s5"]
    assert evidence.values["branch"] == "sealed"
    assert evidence.values["execution_reachable"] is False


def test_s10_requires_prior_close_float_share_fact_available_by_signal_time():
    day = date(2025, 8, 28)
    history = history_before(day, volume=100)
    source_day = day - timedelta(days=1)
    available = datetime.combine(source_day, time(15), tzinfo=CST)
    current = make_day(
        day,
        prices=[101.0] * 240,
        volumes=[300] * 240,
        available_at=available,
        source_day=source_day,
    )
    result = evaluate(current, history)
    evidence = evidence_by_signal(result)
    assert evidence["s10"].qualified is True
    assert evidence["s10"].values["turnover_ratio_vs_prev5_same_minute"] == pytest.approx(3)
    assert evidence["s10"].values["float_shares_source_day"] == source_day.isoformat()
    assert evidence["s10"].values["turnover_availability_basis"] == "previous_daily_market_close"

    post_close = make_day(
        day,
        prices=[101.0] * 240,
        volumes=[300] * 240,
        available_at=datetime.combine(day, time(16), tzinfo=CST),
    )
    censored = evaluate(post_close, history)
    assert "s10" not in evidence_by_signal(censored)
    assert "censor_pit_fact_missing" in censored.censor_codes["s10"]


def test_s10_non_event_is_evidence_not_pit_censor():
    day = date(2025, 8, 28)
    history = history_before(day, volume=100)
    result = evaluate(
        make_day(
            day,
            prices=[99.0] * 240,
            volumes=[100] * 240,
            available_at=datetime.combine(day - timedelta(days=1), time(15), tzinfo=CST),
        ),
        history,
    )
    evidence = evidence_by_signal(result)
    assert evidence["s10"].qualified is False
    assert result.censor_codes["s10"] == ()


def test_intraday_aggregate_uses_signal_price_for_horizon_one():
    day = date(2025, 8, 28)
    bar = Bar(
        SYMBOL,
        day,
        50,
        55.5,
        49.5,
        55,
        100,
        111,
        99,
        110,
        1000,
        10000,
    )
    detection = Detection(
        "escape_s6",
        "intraday_v1",
        SYMBOL,
        day,
        evidence=DetectionEvidence(
            True,
            {
                "execution_session": "same_day",
                "execution_price": 100.0,
                "execution_reachable": True,
            },
        ),
    )
    report = aggregate_escape_signals(
        [detection],
        {SYMBOL: (bar,)},
        horizons=(1,),
        cost_bps=0,
    )
    signal = next(item for item in report.signals if item.signal_id == "s6")
    assert signal.horizons[0].forward_return_mean == pytest.approx(0.10)
