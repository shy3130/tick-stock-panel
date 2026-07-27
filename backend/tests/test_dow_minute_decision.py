from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.dow_minute_decision import (
    MinuteDecisionContext,
    build_minute_decision,
)

ZONE = ZoneInfo("Asia/Hong_Kong")


def context(**overrides) -> MinuteDecisionContext:
    payload = {
        "symbol": "01347.HK",
        "market": "hk",
        "decision_minute": datetime(2026, 7, 27, 10, 26, tzinfo=ZONE),
        "source_timestamp": datetime(2026, 7, 27, 10, 25, tzinfo=ZONE),
        "trends": {},
        "operations": {},
        "completed_minute": True,
        "latest_close": 138.70,
        "support_price": 136.80,
        "resistance_price": 139.20,
        "minute_volume": 320_000,
        "recent_average_volume": 200_000,
        "capital": {},
    }
    payload.update(overrides)
    return MinuteDecisionContext(**payload)


def bullish_context(**overrides) -> MinuteDecisionContext:
    payload = {
        "trends": {
            "5m": "UP",
            "15m": "UP",
            "30m": "UP",
            "60m": "RANGE",
            "day": "UP",
        },
        "operations": {"5m": "买入触发", "15m": "持有", "30m": "持有"},
        "capital": {
            "total_net": 8_000_000,
            "large_net": 2_000_000,
            "flow_15m": 600_000,
            "flow_30m": 1_200_000,
        },
    }
    payload.update(overrides)
    return context(**payload)


def bearish_context(**overrides) -> MinuteDecisionContext:
    payload = {
        "trends": {
            "5m": "DOWN",
            "15m": "DOWN",
            "30m": "DOWN",
            "60m": "RANGE",
            "day": "DOWN",
        },
        "operations": {"5m": "卖出触发", "15m": "卖出触发", "30m": "持有"},
        "capital": {
            "total_net": -8_000_000,
            "large_net": -2_000_000,
            "flow_15m": -600_000,
            "flow_30m": -1_200_000,
        },
    }
    payload.update(overrides)
    return context(**payload)


def test_bullish_structure_with_capital_confirmation_returns_watch_buy() -> None:
    result = build_minute_decision(bullish_context())

    assert result.direction_label == "偏涨"
    assert result.action_label == "买入观察"
    assert result.dominant_timeframe == "15m"
    assert result.confirmation_timeframes == ("30m",)
    assert result.confidence == 92
    assert result.supporting_reasons[:2] == (
        "15/30分钟结构同向偏强",
        "当日资金与大单资金同步净流入",
    )
    assert result.invalidation_conditions == (
        "跌破 136.80 且大单资金转为净流出",
    )


def test_bullish_structure_without_new_trigger_returns_hold() -> None:
    result = build_minute_decision(
        bullish_context(operations={"5m": "观察", "15m": "持有", "30m": "持有"})
    )

    assert result.direction == "BULLISH"
    assert result.action == "HOLD"
    assert result.action_label == "持有"


def test_bearish_structure_with_capital_confirmation_returns_reduce_sell() -> None:
    result = build_minute_decision(bearish_context())

    assert result.direction_label == "偏跌"
    assert result.action_label == "减仓/卖出"
    assert result.confidence == 92
    assert result.supporting_reasons[:2] == (
        "15/30分钟结构同向偏弱",
        "当日资金与大单资金同步净流出",
    )
    assert result.invalidation_conditions == (
        "突破 139.20 且大单资金转为净流入",
    )


def test_conflicting_structure_and_capital_returns_range_observe() -> None:
    result = build_minute_decision(
        context(
            trends={
                "5m": "UP",
                "15m": "UP",
                "30m": "DOWN",
                "60m": "DOWN",
                "day": "UP",
            },
            operations={"5m": "买入触发"},
            capital={
                "total_net": 3_000_000,
                "large_net": -2_000_000,
                "flow_15m": 200_000,
                "flow_30m": -500_000,
            },
        )
    )

    assert result.direction == "RANGE"
    assert result.action == "OBSERVE"
    assert result.confidence == 35
    assert "15/30分钟方向冲突" in result.contrary_risks


@pytest.mark.parametrize("missing_timeframe", ["15m", "30m"])
def test_missing_primary_structure_forces_observe(missing_timeframe: str) -> None:
    trends = dict(bullish_context().trends)
    trends.pop(missing_timeframe)

    result = build_minute_decision(bullish_context(trends=trends))

    assert result.direction == "RANGE"
    assert result.action == "OBSERVE"
    assert result.confidence == 40
    assert result.data_status == "INSUFFICIENT_STRUCTURE"
    assert result.status_label == "关键周期不足"


def test_missing_capital_keeps_direction_but_lowers_confidence() -> None:
    complete = build_minute_decision(bullish_context())
    missing = build_minute_decision(
        bullish_context(
            capital={
                "total_net": None,
                "large_net": None,
                "flow_15m": None,
                "flow_30m": None,
            }
        )
    )

    assert missing.direction == "BULLISH"
    assert missing.data_status == "CAPITAL_UNAVAILABLE"
    assert missing.status_label == "暂无当日资金数据"
    assert missing.confidence == 70
    assert missing.confidence < complete.confidence
    assert "暂无当日资金数据" in missing.contrary_risks


def test_zero_capital_is_neutral_data_not_missing_data() -> None:
    result = build_minute_decision(
        bullish_context(
            capital={
                "total_net": 0,
                "large_net": 0,
                "flow_15m": 0,
                "flow_30m": 0,
            }
        )
    )

    assert result.data_status == "COMPLETE"
    assert result.status_label == "数据完整"
    assert "暂无当日资金数据" not in result.contrary_risks


@pytest.mark.parametrize(
    ("capital_state", "data_status", "status_label", "risk"),
    [
        (
            "DELAYED",
            "CAPITAL_DELAYED",
            "资金数据延迟",
            "资金数据延迟, 暂不作为确认依据",
        ),
        (
            "INSUFFICIENT",
            "CAPITAL_INSUFFICIENT",
            "资金数据点不足",
            "资金数据点不足, 尚不能确认15/30分钟资金",
        ),
    ],
)
def test_capital_quality_is_explained_without_reporting_missing(
    capital_state,
    data_status,
    status_label,
    risk,
) -> None:
    result = build_minute_decision(
        bullish_context(capital_state=capital_state)
    )

    assert result.data_status == data_status
    assert result.status_label == status_label
    assert risk in result.contrary_risks
    assert "暂无当日资金数据" not in result.contrary_risks


def test_delayed_capital_is_not_used_as_a_supporting_reason() -> None:
    result = build_minute_decision(bullish_context(capital_state="DELAYED"))

    assert "当日资金与大单资金同步净流入" not in result.supporting_reasons
    assert "15/30分钟资金持续改善" not in result.supporting_reasons
    assert "资金数据延迟, 暂不作为确认依据" in result.contrary_risks


def test_forming_minute_trigger_cannot_emit_watch_buy() -> None:
    result = build_minute_decision(bullish_context(completed_minute=False))

    assert result.direction == "BULLISH"
    assert result.action == "HOLD"
    assert "最新一分钟K线尚未完成" in result.contrary_risks


def test_capital_divergence_is_explained_and_reduces_confidence() -> None:
    aligned = build_minute_decision(bullish_context())
    diverged = build_minute_decision(
        bullish_context(
            capital={
                "total_net": -8_000_000,
                "large_net": -2_000_000,
                "flow_15m": -600_000,
                "flow_30m": -1_200_000,
            }
        )
    )

    assert diverged.confidence < aligned.confidence
    assert "价格结构偏强但资金方向偏弱" in diverged.contrary_risks
