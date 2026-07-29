from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from datetime import datetime

from app.plugins.clickhouse import bridge
from app.services.dow_monitor_minute_result_models import (
    RawCandlestick,
    RawCapitalSnapshot,
    RawDepthSnapshot,
    RawMinuteHistory,
    RawQuoteSnapshot,
    RawTrade,
)

QueryFn = Callable[[str], list[dict]]


def _clickhouse_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _symbol_tuple(symbols: Sequence[str]) -> str:
    normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
    if not normalized:
        raise ValueError("symbols must not be empty")
    return "(" + ", ".join(_clickhouse_string(symbol) for symbol in normalized) + ")"


def _time_literal(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ClickHouse time range must be timezone-aware")
    return _clickhouse_string(value.isoformat())


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
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

    def load_raw_history(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> RawMinuteHistory:
        if end <= start:
            raise ValueError("end must be later than start")
        symbol_sql = _symbol_tuple(symbols)
        start_sql = _time_literal(start)
        end_sql = _time_literal(end)
        common = f"symbol IN {symbol_sql}"

        quote_rows = self._query(
            f"""
            SELECT symbol, market,
                   toString(snapshot_minute) AS snapshot_time,
                   last_done, prev_close, high, low,
                   toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_quotes
            WHERE {common}
              AND updated_at >= parseDateTime64BestEffort({start_sql})
              AND updated_at < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, updated_at
            """
        )
        depth_rows = self._query(
            f"""
            SELECT symbol, market,
                   toString(snapshot_minute) AS snapshot_time,
                   bid_volume, ask_volume, payload,
                   toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_depth
            WHERE {common}
              AND updated_at >= parseDateTime64BestEffort({start_sql})
              AND updated_at < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, updated_at
            """
        )
        trade_rows = self._query(
            f"""
            SELECT symbol, market, toString(trade_time) AS trade_time,
                   price, volume, direction, toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_trades
            WHERE {common}
              AND trade_time >= parseDateTime64BestEffort({start_sql})
              AND trade_time < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, trade_time, updated_at
            """
        )
        candle_rows = self._query(
            f"""
            SELECT symbol, market, period, toString(bar_time) AS bar_time,
                   open, high, low, close, volume, turnover,
                   toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_candlesticks FINAL
            WHERE {common}
              AND period IN ('min_1', 'min_5', 'min_15', 'min_30')
              AND bar_time >= parseDateTime64BestEffort({start_sql})
              AND bar_time < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, period, bar_time
            """
        )
        capital_rows = self._query(
            f"""
            SELECT symbol, market,
                   toString(snapshot_minute) AS snapshot_time,
                   total_in, total_out, toString(updated_at) AS updated_at
            FROM {self._database}.lb_realtime_capital
            WHERE {common}
              AND updated_at >= parseDateTime64BestEffort({start_sql})
              AND updated_at < parseDateTime64BestEffort({end_sql})
            ORDER BY symbol, updated_at
            """
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
