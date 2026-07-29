from __future__ import annotations

import json
from datetime import datetime, timezone

from app.services.dow_monitor_minute_result_source import DowMonitorMinuteResultSource


UTC = timezone.utc
START = datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
END = datetime(2026, 7, 29, 2, 0, tzinfo=UTC)


class QueryCapture:
    def __init__(self, rows_by_table: dict[str, list[dict]] | None = None) -> None:
        self.sql: list[str] = []
        self.rows_by_table = rows_by_table or {}

    def __call__(self, sql: str) -> list[dict]:
        self.sql.append(sql)
        for table, rows in self.rows_by_table.items():
            if table in sql:
                return rows
        return []


def test_queries_each_raw_table_once_for_the_whole_symbol_batch() -> None:
    capture = QueryCapture()
    source = DowMonitorMinuteResultSource(query_fn=capture)

    source.load_raw_history(["700.HK", "AAPL.US"], START, END)

    tables = (
        "lb_realtime_quotes",
        "lb_realtime_depth",
        "lb_realtime_trades",
        "lb_realtime_candlesticks",
        "lb_realtime_capital",
    )
    assert all(sum(table in sql for sql in capture.sql) == 1 for table in tables)
    assert len(capture.sql) == 5
    assert all("'700.HK'" in sql and "'AAPL.US'" in sql for sql in capture.sql)


def test_uses_final_only_for_replacing_merge_tree_candlesticks() -> None:
    capture = QueryCapture()

    DowMonitorMinuteResultSource(query_fn=capture).load_raw_history(
        ["700.HK"],
        START,
        END,
    )

    candle_sql = next(sql for sql in capture.sql if "lb_realtime_candlesticks" in sql)
    plain_sql = [sql for sql in capture.sql if sql is not candle_sql]
    assert "lb_realtime_candlesticks FINAL" in candle_sql
    assert all(" FINAL" not in sql for sql in plain_sql)


def test_qualifies_timestamp_filters_to_avoid_clickhouse_alias_substitution() -> None:
    capture = QueryCapture()

    DowMonitorMinuteResultSource(query_fn=capture).load_raw_history(
        ["700.HK"],
        START,
        END,
    )

    for table, alias in (
        ("lb_realtime_quotes", "quotes"),
        ("lb_realtime_depth", "depth"),
        ("lb_realtime_capital", "capital"),
    ):
        sql = next(item for item in capture.sql if table in item)
        assert f"FROM longbridge.{table} AS {alias}" in sql
        assert f"{alias}.updated_at >=" in sql
        assert f"{alias}.updated_at <" in sql


def test_parses_raw_rows_and_preserves_five_level_depth() -> None:
    timestamp = "2026-07-29T09:30:59+08:00"
    rows = {
        "lb_realtime_quotes": [{
            "symbol": "700.HK",
            "market": "hk",
            "snapshot_time": timestamp,
            "last_done": 101,
            "prev_close": 100,
            "high": 102,
            "low": 98,
            "updated_at": timestamp,
        }],
        "lb_realtime_depth": [{
            "symbol": "700.HK",
            "market": "hk",
            "snapshot_time": timestamp,
            "bid_volume": 300,
            "ask_volume": 250,
            "payload": json.dumps({
                "bids": [{"volume": value} for value in [100, 80, 60, 40, 20]],
                "asks": [{"volume": value} for value in [70, 60, 50, 40, 30]],
            }),
            "updated_at": timestamp,
        }],
        "lb_realtime_trades": [{
            "symbol": "700.HK",
            "market": "hk",
            "trade_time": timestamp,
            "price": 101,
            "volume": 10,
            "direction": "BUY",
            "updated_at": timestamp,
        }],
        "lb_realtime_candlesticks": [{
            "symbol": "700.HK",
            "market": "hk",
            "period": "min_1",
            "bar_time": "2026-07-29T09:30:00+08:00",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 80,
            "turnover": 8_080,
            "updated_at": timestamp,
        }],
        "lb_realtime_capital": [{
            "symbol": "700.HK",
            "market": "hk",
            "snapshot_time": timestamp,
            "total_in": 60,
            "total_out": 40,
            "updated_at": timestamp,
        }],
    }
    history = DowMonitorMinuteResultSource(
        query_fn=QueryCapture(rows),
    ).load_raw_history(["700.HK"], START, END)

    assert history.quotes[0].last_price == 101
    assert history.depth[0].bid_volumes == (100.0, 80.0, 60.0, 40.0, 20.0)
    assert history.depth[0].ask_volumes == (70.0, 60.0, 50.0, 40.0, 30.0)
    assert history.trades[0].direction == "BUY"
    assert history.candlesticks[0].period == "min_1"
    assert history.capital[0].total_in == 60


def test_candle_warmup_does_not_expand_other_realtime_queries() -> None:
    capture = QueryCapture()
    candle_start = START.replace(day=20)

    DowMonitorMinuteResultSource(query_fn=capture).load_raw_history(
        ["700.HK"],
        START,
        END,
        candle_start=candle_start,
    )

    candle_sql = next(sql for sql in capture.sql if "lb_realtime_candlesticks" in sql)
    other_sql = [sql for sql in capture.sql if "lb_realtime_candlesticks" not in sql]
    assert candle_start.isoformat() in candle_sql
    assert all(candle_start.isoformat() not in sql for sql in other_sql)
    assert all(START.isoformat() in sql for sql in other_sql)
