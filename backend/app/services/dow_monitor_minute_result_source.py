from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from datetime import date, datetime, time
from time import monotonic
from typing import Any, cast
from zoneinfo import ZoneInfo

from app.plugins.clickhouse import bridge
from app.services.dow_monitor_minute_result_models import (
    MinuteResultKey,
    RawCandlestick,
    RawCapitalSnapshot,
    RawDepthSnapshot,
    RawMinuteHistory,
    RawQuoteSnapshot,
    RawTrade,
    normalize_monitor_symbol,
)
from app.services.dow_monitor_models import MonitoredSymbol

QueryFn = Callable[[str], list[dict]]


def _clickhouse_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _symbol_tuple(symbols: Sequence[str]) -> str:
    normalized = sorted(
        {normalize_monitor_symbol(symbol) for symbol in symbols if symbol.strip()}
    )
    if not normalized:
        raise ValueError("symbols must not be empty")
    return "(" + ", ".join(_clickhouse_string(symbol) for symbol in normalized) + ")"


def _time_literal(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ClickHouse time range must be timezone-aware")
    return _clickhouse_string(value.isoformat())


def _number(value: object) -> float | None:
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _payload(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _depth_volumes(payload: object, side: str) -> tuple[float, ...]:
    document = _payload(payload)
    nested = document.get("depth")
    if isinstance(nested, dict):
        document = nested
    candidates = document.get(side)
    if not isinstance(candidates, list):
        candidates = document.get("bid" if side == "bids" else "ask")
    if not isinstance(candidates, list):
        return ()
    values = []
    for row in candidates[:5]:
        value = _number(row.get("volume")) if isinstance(row, dict) else None
        if value is not None:
            values.append(value)
    return tuple(values)


class DowMonitorMinuteResultSource:
    def __init__(
        self,
        query_fn: QueryFn = bridge.query_json_each_row,
        *,
        database: str = "longbridge",
    ) -> None:
        if database != "longbridge":
            raise ValueError("minute result raw source database must be longbridge")
        self._query = query_fn
        self._database = database

    def _query_with_deadline(
        self,
        sql: str,
        deadline: float | None,
    ) -> list[dict]:
        if deadline is None:
            return self._query(sql)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("minute result resource budget exhausted")
        budgeted_sql = (
            f"{sql.rstrip()}\n"
            f"SETTINGS max_execution_time = {max(0.001, remaining):.3f}"
        )
        if self._query is bridge.query_json_each_row:
            return bridge.query_json_each_row(
                budgeted_sql,
                timeout_seconds=remaining,
            )
        return self._query(budgeted_sql)

    def candidate_minute_keys(
        self,
        items: Sequence[MonitoredSymbol],
        market_day: date,
        end: datetime,
        *,
        deadline: float | None = None,
    ) -> set[MinuteResultKey]:
        enabled = [item for item in items if item.enabled]
        if not enabled:
            return set()
        markets = {item.market for item in enabled}
        if len(markets) != 1:
            raise ValueError("candidate minute key query requires one market")
        market = next(iter(markets))
        zones = {
            "cn": ZoneInfo("Asia/Shanghai"),
            "hk": ZoneInfo("Asia/Hong_Kong"),
            "us": ZoneInfo("America/New_York"),
        }
        start = datetime.combine(market_day, time.min, tzinfo=zones[market])
        symbol_sql = _symbol_tuple([item.symbol for item in enabled])
        rows = self._query_with_deadline(
            f"""
            SELECT symbol,
                   toString(bar_time + interval 1 minute) AS decision_minute
            FROM {self._database}.lb_realtime_candlesticks AS candles FINAL
            WHERE symbol IN {symbol_sql}
              AND period = 'min_1'
              AND candles.bar_time >= parseDateTime64BestEffort({_time_literal(start)})
              AND candles.bar_time < parseDateTime64BestEffort({_time_literal(end)})
              AND candles.bar_time + interval 1 minute
                    <= parseDateTime64BestEffort({_time_literal(end)})
              AND isNotNull(open) AND isNotNull(high)
              AND isNotNull(low) AND isNotNull(close) AND isNotNull(volume)
            GROUP BY symbol, bar_time
            ORDER BY symbol, bar_time
            """,
            deadline,
        )
        storage_zone = ZoneInfo("Asia/Shanghai")
        output: set[MinuteResultKey] = set()
        for row in rows:
            value = row.get("decision_minute")
            if value is None:
                continue
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=storage_zone)
            output.add(
                MinuteResultKey(
                    market=market,
                    symbol=normalize_monitor_symbol(str(row.get("symbol") or "")),
                    decision_minute=parsed,
                )
            )
        return output

    def load_raw_history(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        *,
        candle_start: datetime | None = None,
        deadline: float | None = None,
    ) -> RawMinuteHistory:
        if end <= start:
            raise ValueError("end must be later than start")
        candle_start = candle_start or start
        if candle_start > start:
            raise ValueError("candle_start must not be later than start")
        symbol_sql = _symbol_tuple(symbols)
        start_sql = _time_literal(start)
        candle_start_sql = _time_literal(candle_start)
        end_sql = _time_literal(end)
        common = f"symbol IN {symbol_sql}"

        quote_rows = self._query_with_deadline(
            f"""
            SELECT symbol, market,
                   toString(snapshot_minute) AS snapshot_time,
                   last_done, prev_close, high, low,
                   toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_quotes AS quotes
            WHERE {common}
              AND quotes.updated_at >= parseDateTime64BestEffort({start_sql})
              AND quotes.updated_at < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, updated_at
            """,
            deadline,
        )
        depth_rows = self._query_with_deadline(
            f"""
            SELECT symbol, market,
                   toString(snapshot_minute) AS snapshot_time,
                   bid_volume, ask_volume, payload,
                   toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_depth AS depth
            WHERE {common}
              AND depth.updated_at >= parseDateTime64BestEffort({start_sql})
              AND depth.updated_at < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, updated_at
            """,
            deadline,
        )
        trade_rows = self._query_with_deadline(
            f"""
            SELECT symbol, market, toString(trade_time) AS trade_time,
                   price, volume, direction, toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_trades AS trades
            WHERE {common}
              AND trades.trade_time >= parseDateTime64BestEffort({start_sql})
              AND trades.trade_time < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, trade_time, updated_at
            """,
            deadline,
        )
        candle_rows = self._query_with_deadline(
            f"""
            SELECT symbol, market, period, toString(bar_time) AS bar_time,
                   open, high, low, close, volume, turnover,
                   toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_candlesticks AS candles FINAL
            WHERE {common}
              AND period IN ('min_1', 'min_5', 'min_15', 'min_30')
              AND candles.bar_time >= parseDateTime64BestEffort({candle_start_sql})
              AND candles.bar_time < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, period, bar_time
            """,
            deadline,
        )
        capital_rows = self._query_with_deadline(
            f"""
            SELECT symbol, market,
                   toString(snapshot_minute) AS snapshot_time,
                   total_in, total_out, toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_capital AS capital
            WHERE {common}
              AND capital.updated_at >= parseDateTime64BestEffort({start_sql})
              AND capital.updated_at < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, updated_at
            """,
            deadline,
        )

        return RawMinuteHistory(
            quotes=tuple(
                RawQuoteSnapshot(
                    **row,
                    last_price=row.get("last_done"),
                )
                for row in quote_rows
            ),
            depth=tuple(
                RawDepthSnapshot(
                    **row,
                    bid_volumes=_depth_volumes(row.get("payload"), "bids"),
                    ask_volumes=_depth_volumes(row.get("payload"), "asks"),
                )
                for row in depth_rows
            ),
            trades=tuple(RawTrade(**row) for row in trade_rows),
            candlesticks=tuple(RawCandlestick(**row) for row in candle_rows),
            capital=tuple(RawCapitalSnapshot(**row) for row in capital_rows),
        )
