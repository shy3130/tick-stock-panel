from __future__ import annotations

import math
from collections.abc import Sequence

from app.services.dow_monitor_minute_result_models import (
    DowMonitorMinuteResult,
    MinuteBar,
    MinuteResultContext,
    StableTimeframeState,
)

INDICATOR_FIELDS = (
    "channel",
    "control_distance_pct",
    "vwap_distance_pct",
    "momentum_1m_pct",
    "momentum_5m_pct",
    "momentum_15m_pct",
    "volume_ratio",
    "volume_speed",
    "active_buy_ratio",
    "depth_imbalance_pct",
    "distance_to_day_high_pct",
    "distance_to_day_low_pct",
    "atr14_pct",
    "confirmation_count",
)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def percent_change(current: float | None, base: float | None) -> float | None:
    if not _finite(current) or not _finite(base) or base == 0:
        return None
    return (current - base) / base * 100.0


def depth_imbalance_pct(
    bids: Sequence[float] | None,
    asks: Sequence[float] | None,
) -> float | None:
    if not bids or not asks:
        return None
    bid = sum(value for value in bids[:5] if math.isfinite(value))
    ask = sum(value for value in asks[:5] if math.isfinite(value))
    total = bid + ask
    return (bid - ask) / total * 100.0 if total > 0 else None


def _completed_bars(state: StableTimeframeState | None) -> tuple[MinuteBar, ...]:
    if state is None:
        return ()
    if state.bar_completion == "FORMING" or state.provisional:
        return state.bars[:-1]
    return state.bars


def _stable_state(state: StableTimeframeState | None) -> StableTimeframeState | None:
    if state is None or state.bar_completion == "FORMING" or state.provisional:
        return None
    return state


def _bar_channel(state: StableTimeframeState | None) -> str | None:
    bars = _completed_bars(state)
    if not bars:
        return None
    bar = bars[-1]
    if not all(_finite(value) for value in (bar.ma5, bar.ma10, bar.ma20)):
        return None
    if bar.close > bar.ma5 > bar.ma10 > bar.ma20:
        return "UP"
    if bar.close < bar.ma5 < bar.ma10 < bar.ma20:
        return "DOWN"
    return "RANGE"


def _channel(context: MinuteResultContext) -> str | None:
    fifteen = _bar_channel(context.states.get("15m"))
    thirty = _bar_channel(context.states.get("30m"))
    if fifteen is None and thirty is None:
        return None
    if fifteen is None or thirty is None:
        return "PENDING"
    if fifteen == thirty == "UP":
        return "UP"
    if fifteen == thirty == "DOWN":
        return "DOWN"
    return "RANGE"


def _stable_value(context: MinuteResultContext, field: str) -> float | None:
    for timeframe in ("15m", "30m"):
        state = _stable_state(context.states.get(timeframe))
        value = getattr(state, field) if state is not None else None
        if _finite(value):
            return value
    return None


def _momentum(state: StableTimeframeState | None) -> float | None:
    bars = _completed_bars(state)
    if len(bars) < 2:
        return None
    return percent_change(bars[-1].close, bars[-2].close)


def _volume_speed(context: MinuteResultContext) -> float | None:
    bars = _completed_bars(context.states.get("5m"))[-12:]
    if len(bars) != 12 or any(not _finite(bar.volume) or bar.volume < 0 for bar in bars):
        return None
    baseline_per_minute = sum(bar.volume for bar in bars) / 12.0 / 5.0
    if baseline_per_minute <= 0 or context.minute_bar.volume < 0:
        return None
    return context.minute_bar.volume / baseline_per_minute


def _active_buy_ratio(context: MinuteResultContext) -> float | None:
    total_in = context.capital_total_in
    total_out = context.capital_total_out
    if (
        context.capital_quality != "COMPLETE"
        or not _finite(total_in)
        or not _finite(total_out)
        or total_in + total_out <= 0
    ):
        return None
    return total_in / (total_in + total_out) * 100.0


def _day_high_distance(last_price: float | None, high: float | None) -> float | None:
    if not _finite(last_price) or last_price <= 0 or not _finite(high):
        return None
    return max(high - last_price, 0.0) / last_price * 100.0


def _day_low_distance(last_price: float | None, low: float | None) -> float | None:
    if not _finite(last_price) or last_price <= 0 or not _finite(low):
        return None
    return max(last_price - low, 0.0) / last_price * 100.0


def _atr14_pct(state: StableTimeframeState | None) -> float | None:
    bars = _completed_bars(state)
    if len(bars) < 15:
        return None
    true_ranges = [
        max(
            bar.high - bar.low,
            abs(bar.high - bars[index - 1].close),
            abs(bar.low - bars[index - 1].close),
        )
        for index, bar in enumerate(bars[1:], start=1)
    ]
    recent = true_ranges[-14:]
    latest_close = bars[-1].close
    if len(recent) != 14 or latest_close <= 0:
        return None
    return sum(recent) / 14.0 / latest_close * 100.0


def _confirmation_count(context: MinuteResultContext) -> int | None:
    if context.decision_direction is None:
        return None
    if context.decision_direction == "RANGE":
        return 0
    values = {context.dominant_timeframe, *context.confirmation_timeframes}
    return sum(timeframe in values for timeframe in ("15m", "30m"))


def calculate_minute_result(context: MinuteResultContext) -> DowMonitorMinuteResult:
    signal = context.formal_signal
    values: dict[str, object] = {
        "channel": _channel(context),
        "control_distance_pct": _stable_value(context, "price_to_line_pct"),
        "vwap_distance_pct": context.vwap_distance_pct,
        "momentum_1m_pct": percent_change(
            context.minute_bar.close,
            context.minute_bar.open,
        ),
        "momentum_5m_pct": _momentum(context.states.get("5m")),
        "momentum_15m_pct": _momentum(context.states.get("15m")),
        "volume_ratio": _stable_value(context, "volume_ratio_20"),
        "volume_speed": _volume_speed(context),
        "active_buy_ratio": _active_buy_ratio(context),
        "depth_imbalance_pct": depth_imbalance_pct(
            context.bid_volumes,
            context.ask_volumes,
        ),
        "distance_to_day_high_pct": _day_high_distance(
            context.last_price,
            context.day_high,
        ),
        "distance_to_day_low_pct": _day_low_distance(
            context.last_price,
            context.day_low,
        ),
        "atr14_pct": _atr14_pct(context.states.get("15m")),
        "confirmation_count": _confirmation_count(context),
    }
    change_pct = percent_change(context.last_price, context.prev_close)
    missing_fields = tuple(
        field
        for field in (*INDICATOR_FIELDS, "last_price", "prev_close", "change_pct")
        if (values.get(field) if field in values else {
            "last_price": context.last_price,
            "prev_close": context.prev_close,
            "change_pct": change_pct,
        }[field])
        is None
    )
    result_payload = {
        "calculation_version": "v1",
        "market_day": context.market_day.isoformat(),
        "indicators": values,
        "formal_signal": signal.model_dump(mode="json") if signal is not None else None,
    }
    return DowMonitorMinuteResult(
        market=context.market,
        symbol=context.symbol,
        display_symbol=context.display_symbol,
        decision_minute=context.decision_minute,
        source_bar_time=context.source_bar_time,
        backfill=context.backfill,
        last_price=context.last_price,
        prev_close=context.prev_close,
        change_pct=change_pct,
        minute_open=context.minute_bar.open,
        minute_high=context.minute_bar.high,
        minute_low=context.minute_bar.low,
        minute_close=context.minute_bar.close,
        minute_volume=context.minute_bar.volume,
        minute_turnover=context.minute_bar.turnover,
        **values,
        formal_signal_side=signal.side if signal is not None else None,
        formal_signal_stage=signal.stage if signal is not None else None,
        formal_signal_label=signal.label if signal is not None else None,
        formal_signal_time=signal.triggered_at if signal is not None else None,
        formal_signal_event_key=signal.event_key if signal is not None else None,
        data_quality="PARTIAL" if missing_fields else "COMPLETE",
        missing_fields=missing_fields,
        source_timestamps=context.source_timestamps,
        result_payload=result_payload,
        updated_at=context.updated_at,
    )
