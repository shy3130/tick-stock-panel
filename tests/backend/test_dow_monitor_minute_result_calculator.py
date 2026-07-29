from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.dow_monitor_minute_result_calculator import calculate_minute_result
from app.services.dow_monitor_minute_result_models import (
    FormalSignalReference,
    MinuteBar,
    MinuteResultContext,
    StableTimeframeState,
)


UTC = timezone.utc
DECISION_MINUTE = datetime(2026, 7, 29, 1, 31, tzinfo=UTC)
SOURCE_BAR_TIME = DECISION_MINUTE - timedelta(minutes=1)


def _bar(
    close: float,
    *,
    timestamp: datetime,
    volume: float = 500.0,
    high: float | None = None,
    low: float | None = None,
    ma5: float | None = None,
    ma10: float | None = None,
    ma20: float | None = None,
) -> MinuteBar:
    return MinuteBar(
        timestamp=timestamp,
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=volume,
        turnover=close * volume,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
    )


def _stable(
    timeframe: str,
    bars: list[MinuteBar],
    *,
    completion: str = "FINAL",
    provisional: bool = False,
    control_distance: float | None = None,
    volume_ratio: float | None = None,
) -> StableTimeframeState:
    return StableTimeframeState(
        timeframe=timeframe,
        bar_completion=completion,
        provisional=provisional,
        price_to_line_pct=control_distance,
        line_role="SUPPORT",
        volume_ratio_20=volume_ratio,
        bars=tuple(bars),
    )


def _context(**overrides: object) -> MinuteResultContext:
    five_closes = [100.0] * 11 + [104.0]
    five_bars = [
        _bar(
            close,
            timestamp=SOURCE_BAR_TIME - timedelta(minutes=5 * (11 - index)),
        )
        for index, close in enumerate(five_closes)
    ]
    fifteen_bars = [
        _bar(
            101.0 + index,
            timestamp=SOURCE_BAR_TIME - timedelta(minutes=15 * (14 - index)),
            high=102.0 + index,
            low=100.0 + index,
            ma5=100.0 + index,
            ma10=99.0 + index,
            ma20=98.0 + index,
        )
        for index in range(15)
    ]
    thirty_bars = [
        _bar(
            119.0,
            timestamp=SOURCE_BAR_TIME - timedelta(minutes=30),
            ma5=118.0,
            ma10=117.0,
            ma20=116.0,
        ),
        _bar(
            120.0,
            timestamp=SOURCE_BAR_TIME,
            ma5=119.0,
            ma10=118.0,
            ma20=117.0,
        ),
    ]
    values: dict[str, object] = {
        "market": "hk",
        "market_day": date(2026, 7, 29),
        "symbol": "700.HK",
        "display_symbol": "00700.HK",
        "decision_minute": DECISION_MINUTE,
        "source_bar_time": SOURCE_BAR_TIME,
        "backfill": True,
        "minute_bar": MinuteBar(
            timestamp=SOURCE_BAR_TIME,
            open=100.0,
            high=101.5,
            low=99.5,
            close=101.0,
            volume=80.0,
            turnover=8_080.0,
        ),
        "last_price": 101.0,
        "prev_close": 100.0,
        "day_high": 102.0,
        "day_low": 98.0,
        "bid_volumes": (300.0, 200.0),
        "ask_volumes": (100.0, 100.0),
        "capital_total_in": 60.0,
        "capital_total_out": 40.0,
        "capital_quality": "COMPLETE",
        "vwap_distance_pct": 0.19,
        "states": {
            "5m": _stable("5m", five_bars),
            "15m": _stable(
                "15m",
                fifteen_bars,
                control_distance=1.2,
                volume_ratio=1.6,
            ),
            "30m": _stable("30m", thirty_bars),
        },
        "decision_direction": "BULLISH",
        "dominant_timeframe": "15m",
        "confirmation_timeframes": ("30m",),
        "formal_signal": FormalSignalReference(
            side="SELL",
            stage="CONFIRMED",
            label="卖出确认",
            triggered_at=DECISION_MINUTE - timedelta(minutes=2),
            event_key="evt-1",
        ),
        "source_timestamps": {
            "quote": DECISION_MINUTE - timedelta(seconds=1),
            "depth": DECISION_MINUTE - timedelta(seconds=2),
            "capital": DECISION_MINUTE - timedelta(seconds=3),
            "candlestick": SOURCE_BAR_TIME,
        },
        "updated_at": DECISION_MINUTE + timedelta(seconds=5),
    }
    values.update(overrides)
    return MinuteResultContext(**values)


def test_calculates_all_fourteen_indicators_in_authoritative_units() -> None:
    result = calculate_minute_result(_context())

    assert result.change_pct == pytest.approx(1.0)
    assert result.channel == "UP"
    assert result.control_distance_pct == pytest.approx(1.2)
    assert result.vwap_distance_pct == pytest.approx(0.19)
    assert result.momentum_1m_pct == pytest.approx(1.0)
    assert result.momentum_5m_pct == pytest.approx(4.0)
    assert result.momentum_15m_pct == pytest.approx(100 / 114)
    assert result.volume_ratio == pytest.approx(1.6)
    assert result.volume_speed == pytest.approx(0.8)
    assert result.active_buy_ratio == pytest.approx(60.0)
    assert result.depth_imbalance_pct == pytest.approx(300 / 700 * 100)
    assert result.distance_to_day_high_pct == pytest.approx(100 / 101)
    assert result.distance_to_day_low_pct == pytest.approx(300 / 101)
    assert result.atr14_pct == pytest.approx(2 / 115 * 100)
    assert result.confirmation_count == 2
    assert result.data_quality == "COMPLETE"
    assert result.missing_fields == ()


def test_forming_stable_state_falls_back_to_thirty_minutes_not_five() -> None:
    context = _context()
    states = dict(context.states)
    states["5m"] = states["5m"].model_copy(
        update={"price_to_line_pct": -0.8, "volume_ratio_20": 0.9}
    )
    states["15m"] = states["15m"].model_copy(
        update={
            "bar_completion": "FORMING",
            "price_to_line_pct": 1.5,
            "volume_ratio_20": 1.7,
        }
    )
    states["30m"] = states["30m"].model_copy(
        update={"price_to_line_pct": 0.7, "volume_ratio_20": 2.4}
    )

    result = calculate_minute_result(context.model_copy(update={"states": states}))

    assert result.control_distance_pct == pytest.approx(0.7)
    assert result.volume_ratio == pytest.approx(2.4)


def test_missing_depth_and_capital_remain_null_and_are_listed() -> None:
    result = calculate_minute_result(
        _context(
            bid_volumes=None,
            ask_volumes=None,
            capital_total_in=None,
            capital_total_out=None,
            capital_quality="UNAVAILABLE",
        )
    )

    assert result.depth_imbalance_pct is None
    assert result.active_buy_ratio is None
    assert result.data_quality == "PARTIAL"
    assert {"depth_imbalance_pct", "active_buy_ratio"} <= set(result.missing_fields)


def test_realtime_observation_inputs_cannot_change_formal_signal() -> None:
    bid_heavy = calculate_minute_result(
        _context(bid_volumes=(900.0,), ask_volumes=(1.0,))
    )
    ask_heavy = calculate_minute_result(
        _context(bid_volumes=(1.0,), ask_volumes=(900.0,))
    )

    assert bid_heavy.formal_signal_side == "SELL"
    assert ask_heavy.formal_signal_side == "SELL"
    assert bid_heavy.formal_signal_event_key == ask_heavy.formal_signal_event_key == "evt-1"


def test_context_rejects_a_source_timestamp_after_the_decision_minute() -> None:
    with pytest.raises(ValueError, match="future source timestamp"):
        _context(
            source_timestamps={
                "quote": DECISION_MINUTE + timedelta(milliseconds=1),
                "candlestick": SOURCE_BAR_TIME,
            }
        )
