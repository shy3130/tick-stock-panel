from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from inspect import Parameter, signature
from time import monotonic
from typing import Protocol
from zoneinfo import ZoneInfo

from app.services.dow_monitor_client import LongbridgeDowClient
from app.services.dow_monitor_indicators import enrich_dow_chart_bars
from app.services.dow_monitor_minute_result_calculator import percent_change
from app.services.dow_monitor_minute_result_models import (
    FormalSignalReference,
    MinuteBar,
    MinuteResultContext,
    MinuteResultKey,
    RawCandlestick,
    RawMinuteHistory,
    StableTimeframeState,
    normalize_monitor_symbol,
)
from app.services.dow_monitor_models import DowNotification, MonitoredSymbol

STORAGE_ZONE = ZoneInfo("Asia/Shanghai")
MARKET_ZONES = {
    "cn": ZoneInfo("Asia/Shanghai"),
    "hk": ZoneInfo("Asia/Hong_Kong"),
    "us": ZoneInfo("America/New_York"),
}
PERIOD_MINUTES = {"min_5": 5, "min_15": 15, "min_30": 30}
TIMEFRAME_PERIODS = {"5m": "min_5", "15m": "min_15", "30m": "min_30"}
QUOTE_MAX_AGE = timedelta(seconds=90)
DEPTH_MAX_AGE = timedelta(seconds=120)
CAPITAL_MAX_AGE = timedelta(minutes=15)


class StableStateBuilder(Protocol):
    def build(
        self,
        symbol: str,
        timeframe: str,
        bars: tuple[RawCandlestick, ...],
        as_of: datetime,
        *,
        deadline: float | None = None,
    ) -> StableTimeframeState: ...


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=STORAGE_ZONE) if value.tzinfo is None else value


def _required_bar(row: RawCandlestick) -> bool:
    return all(
        value is not None
        for value in (row.open, row.high, row.low, row.close, row.volume)
    )


def _minute_bar(row: RawCandlestick, enriched: dict | None = None) -> MinuteBar:
    values = enriched or {}
    return MinuteBar(
        timestamp=_aware(row.bar_time),
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=float(row.volume),
        turnover=row.turnover,
        ma5=values.get("ma5"),
        ma10=values.get("ma10"),
        ma20=values.get("ma20"),
    )


def _display_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if normalized.endswith(".HK"):
        code = normalized[:-3]
        if code.isdigit():
            return f"{int(code):05d}.HK"
    return normalized


class DowEngineStableStateBuilder:
    def __init__(self, client: LongbridgeDowClient) -> None:
        self._client = client

    def build(
        self,
        symbol: str,
        timeframe: str,
        bars: tuple[RawCandlestick, ...],
        as_of: datetime,
        *,
        deadline: float | None = None,
    ) -> StableTimeframeState:
        payload = [
            {
                "timestamp": _aware(bar.bar_time).isoformat(),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
            for bar in bars
            if _required_bar(bar)
        ]
        if deadline is None:
            result = self._client.evaluate(
                symbol,
                timeframe,
                payload,
                "FINAL",
                as_of,
            )
        else:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("minute-result materialization deadline exceeded")
            result = self._client.evaluate(
                symbol,
                timeframe,
                payload,
                "FINAL",
                as_of,
                timeout_s=remaining,
            )
        engine_bars = [
            bar.model_dump(mode="python")
            for bar in result.bars
        ]
        enriched = enrich_dow_chart_bars(symbol, engine_bars)
        minute_bars = tuple(
            MinuteBar(
                timestamp=datetime.fromisoformat(str(bar["timestamp"])),
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=float(bar["volume"]),
                turnover=None,
                ma5=bar.get("ma5"),
                ma10=bar.get("ma10"),
                ma20=bar.get("ma20"),
            )
            for bar in enriched
        )
        snapshot = result.snapshot
        return StableTimeframeState(
            timeframe=timeframe,
            bar_completion=snapshot.bar_completion,
            provisional=snapshot.provisional,
            price_to_line_pct=snapshot.price_to_line_pct,
            line_role=snapshot.line_role,
            volume_ratio_20=snapshot.volume_ratio_20,
            bars=minute_bars,
        )


class DowMonitorMinuteResultHistoryBuilder:
    def __init__(self, stable_state_builder: StableStateBuilder) -> None:
        self._stable_state_builder = stable_state_builder

    def build_contexts(
        self,
        history: RawMinuteHistory,
        symbol: MonitoredSymbol,
        market_day: date,
        backfill: bool,
        notifications: Sequence[DowNotification],
        decision_minutes: set[datetime] | None = None,
        deadline: float | None = None,
    ) -> list[MinuteResultContext]:
        self._require_budget(deadline)
        normalized = normalize_monitor_symbol(symbol.symbol)
        zone = MARKET_ZONES[symbol.market]
        minute_rows = sorted(
            (
                row
                for row in history.candlesticks
                if normalize_monitor_symbol(row.symbol) == normalized
                and row.period == "min_1"
                and _required_bar(row)
                and _aware(row.bar_time).astimezone(zone).date() == market_day
            ),
            key=lambda row: _aware(row.bar_time),
        )
        period_rows = {
            timeframe: sorted(
                (
                    row
                    for row in history.candlesticks
                    if normalize_monitor_symbol(row.symbol) == normalized
                    and row.period == period
                    and _required_bar(row)
                ),
                key=lambda row: _aware(row.bar_time),
            )
            for timeframe, period in TIMEFRAME_PERIODS.items()
        }
        quotes = self._symbol_rows(history.quotes, normalized, "updated_at")
        depth = self._symbol_rows(history.depth, normalized, "updated_at")
        capital = self._symbol_rows(history.capital, normalized, "updated_at")
        signal_rows = sorted(
            (
                item
                for item in notifications
                if normalize_monitor_symbol(item.symbol) == normalized
            ),
            key=lambda item: item.triggered_at,
        )
        stable_cache: dict[tuple[str, datetime], StableTimeframeState] = {}
        output: list[MinuteResultContext] = []

        for minute_index, row in enumerate(minute_rows):
            self._require_budget(deadline)
            source_bar_time = _aware(row.bar_time)
            decision_minute = source_bar_time + timedelta(minutes=1)
            if (
                decision_minutes is not None
                and decision_minute not in decision_minutes
            ):
                continue
            quote = self._latest_visible(
                quotes,
                decision_minute,
                QUOTE_MAX_AGE,
                "updated_at",
            )
            book = self._latest_visible(
                depth,
                decision_minute,
                DEPTH_MAX_AGE,
                "updated_at",
            )
            funds = self._latest_visible(
                capital,
                decision_minute,
                CAPITAL_MAX_AGE,
                "updated_at",
            )
            states: dict[str, StableTimeframeState] = {}
            source_timestamps = {"candlestick": source_bar_time}
            for timeframe, candidates in period_rows.items():
                self._require_budget(deadline)
                eligible = tuple(
                    candidate
                    for candidate in candidates
                    if self._bucket_end(candidate) <= decision_minute
                    and _aware(candidate.updated_at) <= decision_minute
                )
                if not eligible:
                    continue
                bucket_end = self._bucket_end(eligible[-1])
                cache_key = (timeframe, bucket_end)
                state = stable_cache.get(cache_key)
                if state is None:
                    if timeframe == "5m":
                        state = self._local_stable_state(timeframe, eligible)
                    else:
                        self._require_budget(deadline)
                        state = self._call_with_deadline(
                            self._stable_state_builder.build,
                            normalized,
                            timeframe,
                            eligible,
                            decision_minute,
                            deadline=deadline,
                        )
                    stable_cache[cache_key] = state
                states[timeframe] = state
                source_timestamps[timeframe] = bucket_end

            if quote is not None:
                source_timestamps["quote"] = _aware(quote.updated_at)
            if book is not None:
                source_timestamps["depth"] = _aware(book.updated_at)
            if funds is not None:
                source_timestamps["capital"] = _aware(funds.updated_at)
            formal = self._formal_signal(signal_rows, decision_minute)
            if formal is not None:
                source_timestamps["formal_signal"] = formal.triggered_at
            visible_minutes = minute_rows[: minute_index + 1]
            vwap_distance = self._vwap_distance(
                visible_minutes,
                quote.last_price if quote is not None else None,
            )

            output.append(
                MinuteResultContext(
                    market=symbol.market,
                    market_day=market_day,
                    symbol=normalized,
                    display_symbol=_display_symbol(normalized),
                    decision_minute=decision_minute,
                    source_bar_time=source_bar_time,
                    backfill=backfill,
                    minute_bar=_minute_bar(row),
                    last_price=quote.last_price if quote is not None else None,
                    prev_close=quote.prev_close if quote is not None else None,
                    day_high=quote.high if quote is not None else None,
                    day_low=quote.low if quote is not None else None,
                    bid_volumes=book.bid_volumes if book is not None else None,
                    ask_volumes=book.ask_volumes if book is not None else None,
                    capital_total_in=funds.total_in if funds is not None else None,
                    capital_total_out=funds.total_out if funds is not None else None,
                    capital_quality="COMPLETE" if funds is not None else "UNAVAILABLE",
                    vwap_distance_pct=vwap_distance,
                    states=states,
                    decision_direction=None,
                    dominant_timeframe=None,
                    confirmation_timeframes=(),
                    formal_signal=formal,
                    source_timestamps=source_timestamps,
                    updated_at=datetime.now(UTC),
                )
            )

        return output

    @staticmethod
    def _call_with_deadline(method, *args, deadline: float | None, **kwargs):
        if deadline is not None:
            try:
                parameters = tuple(signature(method).parameters.values())
            except (TypeError, ValueError):
                parameters = ()
            if any(
                parameter.name == "deadline"
                or parameter.kind == Parameter.VAR_KEYWORD
                for parameter in parameters
            ):
                kwargs["deadline"] = deadline
        return method(*args, **kwargs)

    @staticmethod
    def _require_budget(deadline: float | None) -> None:
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError("minute result resource budget exhausted")

    def candidate_keys(
        self,
        history: RawMinuteHistory,
        symbol: MonitoredSymbol,
        market_day: date,
    ) -> set[MinuteResultKey]:
        normalized = normalize_monitor_symbol(symbol.symbol)
        zone = MARKET_ZONES[symbol.market]
        return {
            MinuteResultKey(
                market=symbol.market,
                symbol=normalized,
                decision_minute=_aware(row.bar_time) + timedelta(minutes=1),
            )
            for row in history.candlesticks
            if normalize_monitor_symbol(row.symbol) == normalized
            and row.period == "min_1"
            and _required_bar(row)
            and _aware(row.bar_time).astimezone(zone).date() == market_day
        }

    @staticmethod
    def _symbol_rows(rows: Sequence, symbol: str, time_field: str) -> list:
        return sorted(
            (
                row
                for row in rows
                if normalize_monitor_symbol(row.symbol) == symbol
            ),
            key=lambda row: _aware(getattr(row, time_field)),
        )

    @staticmethod
    def _latest_visible(
        rows: Sequence,
        decision_minute: datetime,
        max_age: timedelta,
        time_field: str,
    ):
        eligible = [
            row
            for row in rows
            if _aware(getattr(row, time_field)) <= decision_minute
        ]
        if not eligible:
            return None
        latest = eligible[-1]
        timestamp = _aware(getattr(latest, time_field))
        return latest if decision_minute - timestamp <= max_age else None

    @staticmethod
    def _bucket_end(row: RawCandlestick) -> datetime:
        return _aware(row.bar_time) + timedelta(minutes=PERIOD_MINUTES[row.period])

    @staticmethod
    def _local_stable_state(
        timeframe: str,
        rows: tuple[RawCandlestick, ...],
    ) -> StableTimeframeState:
        payload = [
            {
                "timestamp": _aware(row.bar_time).isoformat(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
            }
            for row in rows
        ]
        enriched = enrich_dow_chart_bars(rows[-1].symbol, payload)
        return StableTimeframeState(
            timeframe=timeframe,
            bar_completion="FINAL",
            provisional=False,
            bars=tuple(
                _minute_bar(row, indicators)
                for row, indicators in zip(rows, enriched, strict=True)
            ),
        )

    @staticmethod
    def _formal_signal(
        notifications: Sequence[DowNotification],
        decision_minute: datetime,
    ) -> FormalSignalReference | None:
        eligible = [
            item
            for item in notifications
            if _aware(item.triggered_at) <= decision_minute
        ]
        if not eligible:
            return None
        item = eligible[-1]
        return FormalSignalReference(
            side=item.side,
            stage="CONFIRMED",
            label=item.action_name,
            triggered_at=_aware(item.triggered_at),
            event_key=item.event_key,
        )

    @staticmethod
    def _vwap_distance(
        rows: Sequence[RawCandlestick],
        last_price: float | None,
    ) -> float | None:
        usable = [
            row
            for row in rows
            if row.turnover is not None
            and row.volume is not None
            and row.volume >= 0
        ]
        volume = sum(float(row.volume) for row in usable)
        if volume <= 0:
            return None
        vwap = sum(float(row.turnover) for row in usable) / volume
        return percent_change(last_price, vwap)
