from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

from app.services.dow_monitor_models import DowMinuteDecision

Timeframe = Literal["5m", "15m", "30m", "60m", "day"]
Trend = Literal["UP", "DOWN", "RANGE", "UNKNOWN"]

TIMEFRAME_WEIGHTS: dict[Timeframe, int] = {
    "5m": 1,
    "15m": 3,
    "30m": 3,
    "60m": 2,
    "day": 1,
}
CAPITAL_WEIGHTS = {
    "total_net": 2,
    "large_net": 1,
    "flow_15m": 1,
    "flow_30m": 1,
}
PRIMARY_TIMEFRAMES: tuple[Timeframe, Timeframe] = ("15m", "30m")
CAPITAL_FIELDS = tuple(CAPITAL_WEIGHTS)


class MinuteDecisionContext(BaseModel):
    symbol: str
    market: Literal["cn", "hk", "us"]
    decision_minute: datetime
    source_timestamp: datetime | None
    trends: dict[str, Trend]
    operations: dict[str, str]
    completed_minute: bool
    latest_close: float | None
    support_price: float | None
    resistance_price: float | None
    minute_volume: float | None
    recent_average_volume: float | None
    capital: dict[str, float | None]

    @field_validator("decision_minute")
    @classmethod
    def require_timezone_aware_decision_minute(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision_minute must be timezone-aware")
        return value


def _trend_vote(trend: str | None) -> int:
    if trend == "UP":
        return 1
    if trend == "DOWN":
        return -1
    return 0


def _number_vote(value: float | None) -> int:
    if value is None or value == 0:
        return 0
    return 1 if value > 0 else -1


def _direction_for_score(score: int) -> Literal["BULLISH", "BEARISH", "RANGE"]:
    if score >= 4:
        return "BULLISH"
    if score <= -4:
        return "BEARISH"
    return "RANGE"


def _primary_structure_missing(context: MinuteDecisionContext) -> bool:
    return any(
        context.trends.get(timeframe) not in {"UP", "DOWN", "RANGE"}
        for timeframe in PRIMARY_TIMEFRAMES
    )


def _primary_conflict(context: MinuteDecisionContext) -> bool:
    return {
        context.trends.get("15m"),
        context.trends.get("30m"),
    } == {"UP", "DOWN"}


def _capital_missing(context: MinuteDecisionContext) -> bool:
    return all(context.capital.get(field) is None for field in CAPITAL_FIELDS)


def _capital_internal_conflict(context: MinuteDecisionContext) -> bool:
    votes = {
        vote
        for field in CAPITAL_FIELDS
        if (vote := _number_vote(context.capital.get(field))) != 0
    }
    return votes == {-1, 1}


def _timeframe_scores(context: MinuteDecisionContext) -> tuple[int, dict[str, int]]:
    votes = {
        timeframe: _trend_vote(context.trends.get(timeframe)) * weight
        for timeframe, weight in TIMEFRAME_WEIGHTS.items()
    }
    return sum(votes.values()), votes


def _capital_score(context: MinuteDecisionContext) -> int:
    return sum(
        _number_vote(context.capital.get(field)) * weight
        for field, weight in CAPITAL_WEIGHTS.items()
    )


def _confirmed(
    context: MinuteDecisionContext,
    direction: Literal["BULLISH", "BEARISH", "RANGE"],
    capital_score: int,
) -> bool:
    if direction == "RANGE":
        return False
    direction_vote = 1 if direction == "BULLISH" else -1
    primary_votes = [_trend_vote(context.trends.get(item)) for item in PRIMARY_TIMEFRAMES]
    primary_supports = direction_vote in primary_votes
    primary_opposes = -direction_vote in primary_votes
    background_supports = any(
        _trend_vote(context.trends.get(item)) == direction_vote
        for item in ("60m", "day")
    )
    capital_supports = (
        capital_score > 0 if direction == "BULLISH" else capital_score < 0
    )
    return primary_supports and not primary_opposes and (
        background_supports or capital_supports
    )


def _new_trigger(
    context: MinuteDecisionContext,
    direction: Literal["BULLISH", "BEARISH", "RANGE"],
) -> bool:
    if not context.completed_minute:
        return False
    expected = "买入触发" if direction == "BULLISH" else "卖出触发"
    return any(
        context.operations.get(timeframe) == expected
        for timeframe in ("5m", "15m")
    )


def _dominant_timeframes(
    context: MinuteDecisionContext,
    direction: Literal["BULLISH", "BEARISH", "RANGE"],
) -> tuple[Timeframe | None, tuple[Timeframe, ...]]:
    if direction == "RANGE":
        return None, ()
    expected = "UP" if direction == "BULLISH" else "DOWN"
    matching = [
        timeframe
        for timeframe in PRIMARY_TIMEFRAMES
        if context.trends.get(timeframe) == expected
    ]
    if not matching:
        return None, ()
    dominant = matching[0]
    return dominant, tuple(matching[1:])


def _supporting_reasons(
    context: MinuteDecisionContext,
    direction: Literal["BULLISH", "BEARISH", "RANGE"],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if context.trends.get("15m") == context.trends.get("30m") == "UP":
        reasons.append("15/30分钟结构同向偏强")
    elif context.trends.get("15m") == context.trends.get("30m") == "DOWN":
        reasons.append("15/30分钟结构同向偏弱")

    total_vote = _number_vote(context.capital.get("total_net"))
    large_vote = _number_vote(context.capital.get("large_net"))
    if total_vote == large_vote == 1:
        reasons.append("当日资金与大单资金同步净流入")
    elif total_vote == large_vote == -1:
        reasons.append("当日资金与大单资金同步净流出")

    flow15_vote = _number_vote(context.capital.get("flow_15m"))
    flow30_vote = _number_vote(context.capital.get("flow_30m"))
    if flow15_vote == flow30_vote == 1:
        reasons.append("15/30分钟资金持续改善")
    elif flow15_vote == flow30_vote == -1:
        reasons.append("15/30分钟资金持续走弱")

    if (
        context.minute_volume is not None
        and context.recent_average_volume is not None
        and context.recent_average_volume > 0
        and context.minute_volume / context.recent_average_volume >= 1.2
        and direction != "RANGE"
    ):
        reasons.append("最新完整分钟量能高于近期均量")
    return tuple(reasons[:3])


def _contrary_risks(
    context: MinuteDecisionContext,
    direction: Literal["BULLISH", "BEARISH", "RANGE"],
    structure_direction: Literal["BULLISH", "BEARISH", "RANGE"],
    capital_score: int,
) -> tuple[str, ...]:
    risks: list[str] = []
    if _primary_conflict(context):
        risks.append("15/30分钟方向冲突")
    if _capital_internal_conflict(context):
        risks.append("资金分项方向冲突")
    if structure_direction == "BULLISH" and capital_score < 0:
        risks.append("价格结构偏强但资金方向偏弱")
    elif structure_direction == "BEARISH" and capital_score > 0:
        risks.append("价格结构偏弱但资金方向偏强")
    if not context.completed_minute:
        risks.append("最新一分钟K线尚未完成")
    if _capital_missing(context):
        risks.append("缺少当日资金确认")

    if direction == "BULLISH" and any(
        context.trends.get(item) == "DOWN" for item in ("60m", "day")
    ):
        risks.append("高周期仍有下行约束")
    elif direction == "BEARISH" and any(
        context.trends.get(item) == "UP" for item in ("60m", "day")
    ):
        risks.append("高周期仍有上行支撑")
    return tuple(dict.fromkeys(risks))[:3]


def _invalidation_conditions(
    context: MinuteDecisionContext,
    direction: Literal["BULLISH", "BEARISH", "RANGE"],
) -> tuple[str, ...]:
    if direction == "BULLISH":
        if context.support_price is not None:
            return (
                f"跌破 {context.support_price:.2f} 且大单资金转为净流出",
            )
        return ("15分钟结构转弱且资金转为净流出",)
    if direction == "BEARISH":
        if context.resistance_price is not None:
            return (
                f"突破 {context.resistance_price:.2f} 且大单资金转为净流入",
            )
        return ("15分钟结构转强且资金转为净流入",)
    return ("15/30分钟结构重新同向并获得资金确认",)


def build_minute_decision(context: MinuteDecisionContext) -> DowMinuteDecision:
    if _primary_structure_missing(context):
        return DowMinuteDecision(
            symbol=context.symbol,
            market=context.market,
            decision_minute=context.decision_minute,
            direction="RANGE",
            direction_label="震荡",
            action="OBSERVE",
            action_label="继续观察",
            confidence=40,
            dominant_timeframe=None,
            supporting_reasons=(),
            contrary_risks=("缺少15或30分钟结构",),
            invalidation_conditions=("等待15/30分钟结构完整后重新判断",),
            data_status="INSUFFICIENT_STRUCTURE",
            status_label="关键周期不足",
            source_timestamp=context.source_timestamp,
        )

    structure_score, _ = _timeframe_scores(context)
    capital_score = _capital_score(context)
    total_score = structure_score + capital_score
    structure_direction = _direction_for_score(structure_score)
    direction = _direction_for_score(total_score)

    conflict_penalty = 0
    if _primary_conflict(context):
        conflict_penalty += 12
    if _capital_internal_conflict(context):
        conflict_penalty += 12
    if (
        (structure_direction == "BULLISH"
        and capital_score < 0)
        or (structure_direction == "BEARISH"
        and capital_score > 0)
    ):
        conflict_penalty += 6
    if not context.completed_minute:
        conflict_penalty += 5
    missing_capital = _capital_missing(context)
    if missing_capital:
        conflict_penalty += 12
    confidence = max(35, min(92, 50 + abs(total_score) * 4 - conflict_penalty))

    confirmed = _confirmed(context, direction, capital_score)
    triggered = _new_trigger(context, direction)
    if direction == "BULLISH" and confirmed and triggered:
        action = "WATCH_BUY"
        action_label = "买入观察"
    elif direction == "BULLISH" and confirmed:
        action = "HOLD"
        action_label = "持有"
    elif direction == "BEARISH" and confirmed:
        action = "REDUCE_SELL"
        action_label = "减仓/卖出"
    else:
        action = "OBSERVE"
        action_label = "继续观察"

    direction_label = {
        "BULLISH": "偏涨",
        "BEARISH": "偏跌",
        "RANGE": "震荡",
    }[direction]
    dominant, confirmations = _dominant_timeframes(context, direction)
    return DowMinuteDecision(
        symbol=context.symbol,
        market=context.market,
        decision_minute=context.decision_minute,
        direction=direction,
        direction_label=direction_label,
        action=action,
        action_label=action_label,
        confidence=confidence,
        dominant_timeframe=dominant,
        confirmation_timeframes=confirmations,
        supporting_reasons=_supporting_reasons(context, direction),
        contrary_risks=_contrary_risks(
            context,
            direction,
            structure_direction,
            capital_score,
        ),
        invalidation_conditions=_invalidation_conditions(context, direction),
        data_status="CAPITAL_UNCONFIRMED" if missing_capital else "COMPLETE",
        status_label="资金未确认" if missing_capital else "数据完整",
        source_timestamp=context.source_timestamp,
    )
