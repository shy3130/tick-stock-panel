from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import polars as pl

from app.services.dow_monitor_minute_result_calculator import percent_change
from app.services.dow_monitor_minute_result_models import (
    FormalSignalReference,
    MinuteBar,
    MinuteResultContext,
    StableTimeframeState,
    normalize_monitor_symbol,
)
from app.services.dow_monitor_models import (
    DowMinuteDecision,
    DowNotification,
    DowTimeframeState,
    MonitoredSymbol,
)

MARKET_ZONES = {
    "cn": ZoneInfo("Asia/Shanghai"),
    "hk": ZoneInfo("Asia/Hong_Kong"),
    "us": ZoneInfo("America/New_York"),
}
StableTimeframe = Literal["5m", "15m", "30m"]


def build_live_minute_result_context(
    *,
    item: MonitoredSymbol,
    decision: DowMinuteDecision,
    minute_rows: pl.DataFrame,
    states: Sequence[DowTimeframeState],
    quote: Mapping[str, object] | None,
    depth: Mapping[str, object] | None,
    capital: Mapping[str, object] | None,
    notifications: Sequence[DowNotification],
    updated_at: datetime | None = None,
) -> MinuteResultContext:
    """Build one causal context using only inputs already loaded by the live cycle."""
    decision_minute = _aware(decision.decision_minute)
    source_bar_time = _aware(decision.source_timestamp or decision_minute)
    visible_rows = _visible_minute_rows(minute_rows, item.symbol, source_bar_time)
    if not visible_rows:
        raise ValueError("completed minute bar is unavailable for live append")
    minute_bar = _minute_bar(visible_rows[-1], source_bar_time)
    quote_values = dict(quote or {})
    depth_values = dict(depth or {})
    capital_values = dict(capital or {})
    last_price = _number(
        quote_values.get("last")
        or quote_values.get("last_price")
        or quote_values.get("last_done")
        or quote_values.get("price")
        or minute_bar.close
    )
    stable_states = _stable_states(states, item.symbol, decision_minute)
    formal = _formal_signal(notifications, item.symbol, decision_minute)
    source_timestamps: dict[str, datetime] = {"candlestick": source_bar_time}
    _add_visible_timestamp(
        source_timestamps,
        "quote",
        quote_values.get("updated_at")
        or quote_values.get("timestamp")
        or quote_values.get("snapshot_time"),
        decision_minute,
    )
    _add_visible_timestamp(
        source_timestamps,
        "depth",
        depth_values.get("updated_at") or depth_values.get("snapshot_time"),
        decision_minute,
    )
    _add_visible_timestamp(
        source_timestamps,
        "capital",
        capital_values.get("capital_minute")
        or capital_values.get("updated_at")
        or capital_values.get("snapshot_time"),
        decision_minute,
    )
    for timeframe, state in stable_states.items():
        if state.bars:
            bucket = _aware(state.bars[-1].timestamp)
            if bucket <= decision_minute:
                source_timestamps[timeframe] = bucket
    if formal is not None:
        source_timestamps["formal_signal"] = formal.triggered_at

    normalized = normalize_monitor_symbol(item.symbol)
    return MinuteResultContext(
        market=item.market,
        market_day=source_bar_time.astimezone(MARKET_ZONES[item.market]).date(),
        symbol=normalized,
        display_symbol=_display_symbol(normalized),
        decision_minute=decision_minute,
        source_bar_time=source_bar_time,
        backfill=False,
        minute_bar=minute_bar,
        last_price=last_price,
        prev_close=_number(quote_values.get("prev_close")),
        day_high=_number(quote_values.get("high")),
        day_low=_number(quote_values.get("low")),
        bid_volumes=_volumes(
            depth_values.get("bid_volumes") or depth_values.get("bid_volume")
        ),
        ask_volumes=_volumes(
            depth_values.get("ask_volumes") or depth_values.get("ask_volume")
        ),
        capital_total_in=_number(capital_values.get("total_in")),
        capital_total_out=_number(capital_values.get("total_out")),
        capital_quality=str(capital_values.get("quality") or "UNAVAILABLE"),
        vwap_distance_pct=_vwap_distance(visible_rows, last_price),
        states=stable_states,
        decision_direction=decision.direction,
        dominant_timeframe=decision.dominant_timeframe,
        confirmation_timeframes=decision.confirmation_timeframes,
        formal_signal=formal,
        source_timestamps=source_timestamps,
        updated_at=_aware(updated_at or datetime.now(UTC)),
    )


def _stable_states(
    states: Sequence[DowTimeframeState],
    symbol: str,
    decision_minute: datetime,
) -> dict[StableTimeframe, StableTimeframeState]:
    output: dict[StableTimeframe, StableTimeframeState] = {}
    normalized_symbol = normalize_monitor_symbol(symbol)
    for state in states:
        if (
            normalize_monitor_symbol(state.symbol) != normalized_symbol
            or state.timeframe not in {"5m", "15m", "30m"}
        ):
            continue
        snapshot = state.snapshot if isinstance(state.snapshot, dict) else {}
        bars: list[MinuteBar] = []
        raw_bars = state.chart.get("bars") if isinstance(state.chart, dict) else None
        for raw in raw_bars if isinstance(raw_bars, list) else []:
            if not isinstance(raw, dict):
                continue
            timestamp = _parse_time(raw.get("timestamp"))
            if timestamp is None or timestamp > decision_minute:
                continue
            required = [
                _number(raw.get(name))
                for name in ("open", "high", "low", "close", "volume")
            ]
            if any(value is None for value in required):
                continue
            bars.append(
                MinuteBar(
                    timestamp=timestamp,
                    open=cast(float, required[0]),
                    high=cast(float, required[1]),
                    low=cast(float, required[2]),
                    close=cast(float, required[3]),
                    volume=cast(float, required[4]),
                    turnover=_number(raw.get("turnover") or raw.get("amount")),
                    ma5=_number(raw.get("ma5")),
                    ma10=_number(raw.get("ma10")),
                    ma20=_number(raw.get("ma20")),
                )
            )
        timeframe = cast(StableTimeframe, state.timeframe)
        output[timeframe] = StableTimeframeState(
            timeframe=timeframe,
            bar_completion=str(snapshot.get("bar_completion") or "FINAL"),
            provisional=bool(snapshot.get("provisional", False)),
            price_to_line_pct=_number(snapshot.get("price_to_line_pct")),
            line_role=(
                str(snapshot["line_role"])
                if snapshot.get("line_role") is not None
                else None
            ),
            volume_ratio_20=_number(snapshot.get("volume_ratio_20")),
            bars=tuple(bars),
        )
    return output


def _visible_minute_rows(
    rows: pl.DataFrame,
    symbol: str,
    source_bar_time: datetime,
) -> list[dict]:
    if rows.is_empty():
        return []
    normalized = normalize_monitor_symbol(symbol)
    visible: list[tuple[datetime, dict]] = []
    for row in rows.to_dicts():
        if normalize_monitor_symbol(str(row.get("symbol") or "")) != normalized:
            continue
        timestamp = _parse_time(row.get("datetime") or row.get("bar_time"))
        if timestamp is None or timestamp > source_bar_time:
            continue
        visible.append((timestamp, row))
    return [row for _timestamp, row in sorted(visible, key=lambda item: item[0])]


def _minute_bar(row: Mapping[str, object], timestamp: datetime) -> MinuteBar:
    required = [
        _number(row.get(name)) for name in ("open", "high", "low", "close", "volume")
    ]
    if any(value is None for value in required):
        raise ValueError("completed minute bar has missing OHLCV")
    return MinuteBar(
        timestamp=timestamp,
        open=cast(float, required[0]),
        high=cast(float, required[1]),
        low=cast(float, required[2]),
        close=cast(float, required[3]),
        volume=cast(float, required[4]),
        turnover=_number(row.get("turnover") or row.get("amount")),
        ma5=_number(row.get("ma5")),
        ma10=_number(row.get("ma10")),
        ma20=_number(row.get("ma20")),
    )


def _vwap_distance(
    rows: Sequence[Mapping[str, object]],
    last_price: float | None,
) -> float | None:
    usable: list[tuple[float, float]] = []
    for row in rows:
        volume = _number(row.get("volume"))
        turnover = _number(row.get("turnover") or row.get("amount"))
        if volume is None or turnover is None or volume < 0:
            continue
        usable.append((volume, turnover))
    total_volume = sum(volume for volume, _turnover in usable)
    if total_volume <= 0:
        return None
    vwap = sum(turnover for _volume, turnover in usable) / total_volume
    return percent_change(last_price, vwap)


def _formal_signal(
    notifications: Sequence[DowNotification],
    symbol: str,
    decision_minute: datetime,
) -> FormalSignalReference | None:
    normalized = normalize_monitor_symbol(symbol)
    eligible = [
        item
        for item in notifications
        if normalize_monitor_symbol(item.symbol) == normalized
        and _aware(item.triggered_at) <= decision_minute
    ]
    if not eligible:
        return None
    latest = max(eligible, key=lambda item: _aware(item.triggered_at))
    return FormalSignalReference(
        side=latest.side,
        stage="CONFIRMED",
        label=latest.action_name,
        triggered_at=_aware(latest.triggered_at),
        event_key=latest.event_key,
    )


def _add_visible_timestamp(
    output: dict[str, datetime],
    name: str,
    value: object,
    decision_minute: datetime,
) -> None:
    parsed = _parse_time(value)
    if parsed is not None and parsed <= decision_minute:
        output[name] = parsed


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, (int, float)):
        seconds = (
            float(value) / 1_000.0
            if abs(float(value)) > 10_000_000_000
            else float(value)
        )
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str) and value.strip():
        try:
            return _aware(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        if value.tzinfo is None
        else value
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(cast(Any, value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _volumes(value: object) -> tuple[float, ...] | None:
    if isinstance(value, (list, tuple)):
        output = tuple(
            number for item in value if (number := _number(item)) is not None
        )
        return output or None
    number = _number(value)
    return (number,) if number is not None else None


def _display_symbol(symbol: str) -> str:
    if symbol.endswith(".HK") and symbol[:-3].isdigit():
        return f"{int(symbol[:-3]):05d}.HK"
    return symbol
