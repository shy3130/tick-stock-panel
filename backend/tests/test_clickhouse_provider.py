from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from app.plugins.clickhouse import provider as provider_module
from app.plugins.clickhouse.provider import ClickHouseProvider


class QueryRecorder:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def __call__(self, sql: str) -> list[dict]:
        self.queries.append(sql)
        return self.rows


def test_longbridge_daily_fallback_stays_within_sdk_kline_limit(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"bars": []}

    def fake_get(_url: str, *, params: dict, timeout: float):
        captured.update(params=params, timeout=timeout)
        return Response()

    monkeypatch.setenv("LONGBRIDGE_API_URL", "http://longbridge")
    monkeypatch.setattr(provider_module.httpx, "get", fake_get)

    provider_module._fetch_longbridge_daily(".SPX.US")

    assert captured["params"]["limit"] == 1000


def test_daily_maps_turnover_to_amount_and_filters_adjusted() -> None:
    query = QueryRecorder([
        {
            "symbol": "1.HK",
            "trade_date": "2026-07-17",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 1000,
            "turnover": 10500,
            "market": "hk",
        }
    ])
    provider = ClickHouseProvider(query_fn=query)

    frame = provider.get_daily(["1.HK"], None, None)

    assert frame["symbol"].to_list() == ["1.HK"]
    assert frame["amount"].to_list() == [10500.0]
    assert "adjusted = 1" in query.queries[-1].lower()
    assert "'1.HK'" in query.queries[-1]


def test_daily_maps_unpadded_webstock_hk_symbol_to_requested_symbol() -> None:
    query = QueryRecorder([{
        "symbol": "1347.HK",
        "trade_date": "2026-07-22",
        "open": 160,
        "high": 164,
        "low": 158,
        "close": 160.9,
        "volume": 49_800_238,
        "turnover": 8_219_757_554.55,
        "market": "hk",
    }])
    provider = ClickHouseProvider(query_fn=query)

    frame = provider.get_daily(["01347.HK"], None, None)

    assert frame.get_column("symbol").to_list() == ["01347.HK"]
    assert "symbol IN ('01347.HK', '1347.HK')" in query.queries[-1]


def test_daily_uses_longbridge_fallback_for_symbols_missing_from_clickhouse() -> None:
    provider = ClickHouseProvider(query_fn=QueryRecorder([]))
    requested: list[str] = []

    def fallback(symbol: str) -> list[dict]:
        requested.append(symbol)
        return [{
            "date": "2026-07-17",
            "open": 6300,
            "high": 6350,
            "low": 6250,
            "close": 6320,
            "volume": 100,
            "turnover": 632000,
        }]

    provider._daily_fallback_fn = fallback

    frame = provider.get_daily([".SPX.US"], None, None, asset_type="index")

    assert requested == [".SPX.US"]
    assert frame.select("symbol", "close").to_dicts() == [{"symbol": ".SPX.US", "close": 6320.0}]


def test_daily_does_not_fallback_each_missing_cn_index_to_longbridge() -> None:
    provider = ClickHouseProvider(query_fn=QueryRecorder([]))
    requested: list[str] = []
    provider._daily_fallback_fn = lambda symbol: requested.append(symbol) or []

    frame = provider.get_daily(["000004.SH"], None, None, asset_type="index")

    assert frame.is_empty()
    assert requested == []


def test_realtime_normalizes_percentage_and_timestamp() -> None:
    query = QueryRecorder([
        {
            "symbol": "NBIS.US",
            "market": "us",
            "snapshot_minute": "2026-07-18 06:19:00.000",
            "last_done": 177.71,
            "prev_close": 171.77,
            "open": 172.0,
            "high": 178.0,
            "low": 170.0,
            "change_value": 5.94,
            "change_percentage": 3.4581,
            "volume": 1000,
            "turnover": 177710,
        }
    ])
    provider = ClickHouseProvider(query_fn=query)

    rows = provider.get_realtime()

    assert rows[0]["last_price"] == 177.71
    assert rows[0]["amount"] == 177710.0
    assert rows[0]["change_pct"] == 0.034581
    expected = datetime(2026, 7, 17, 22, 19, tzinfo=UTC)
    assert rows[0]["timestamp"] == int(expected.timestamp() * 1000)
    assert "limit 1 by symbol" in query.queries[-1].lower()


def test_realtime_strict_joins_existing_symbol_metadata_without_name_fallback() -> None:
    query = QueryRecorder([
        {
            "symbol": "NBIS.US",
            "name": "Nebius Group",
            "market": "us",
            "snapshot_minute": "2026-07-18 06:19:00.000",
            "last_done": 177.71,
            "prev_close": 171.77,
            "open": 172.0,
            "high": 178.0,
            "low": 170.0,
            "change_value": 5.94,
            "change_percentage": 3.4581,
            "volume": 1000,
            "turnover": 177710,
        }
    ])
    provider = ClickHouseProvider(query_fn=query)

    named = provider.get_realtime_strict(["NBIS.US"])
    query.rows = [{**query.rows[0], "symbol": "UNKNOWN.US", "name": None}]
    unnamed = provider.get_realtime_strict(["UNKNOWN.US"])

    assert named[0]["name"] == "Nebius Group"
    assert unnamed[0]["name"] is None
    assert "longbridge.lb_symbols" in query.queries[0]
    assert "argMax(name, updated_at) AS name" in query.queries[0]


def test_realtime_limits_latest_rows_to_current_shanghai_date() -> None:
    query = QueryRecorder([])
    provider = ClickHouseProvider(query_fn=query)

    provider.get_realtime(symbols=["000001.SZ"])

    sql = query.queries[-1]
    assert "snapshot_minute >= toStartOfDay(now('Asia/Shanghai'))" in sql
    assert "snapshot_minute < toStartOfDay(now('Asia/Shanghai')) + INTERVAL 1 DAY" in sql
    assert "AND symbol IN ('000001.SZ')" in sql


def test_minute_bars_are_returned_in_market_local_time() -> None:
    query = QueryRecorder([
        {
            "symbol": "A.US",
            "market": "us",
            "bar_time_utc": "2026-07-17 20:00:00",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
            "amount": 1050,
        }
    ])
    provider = ClickHouseProvider(query_fn=query)

    frame = provider.get_minute(["A.US"], None, None, freq="1m")

    assert frame["datetime"].to_list() == [datetime(2026, 7, 17, 16, 0)]
    assert frame["amount"].to_list() == [1050.0]
    assert "frequency = '1m'" in query.queries[-1].lower()


def test_minute_bars_filter_by_market_local_trade_date() -> None:
    query = QueryRecorder([
        {
            "symbol": "NBIS.US",
            "market": "us",
            "bar_time_utc": "2026-07-16 19:59:00",
            "open": 170,
            "high": 171,
            "low": 169,
            "close": 170.5,
            "volume": 100,
            "amount": 17050,
        },
        {
            "symbol": "NBIS.US",
            "market": "us",
            "bar_time_utc": "2026-07-17 13:30:00",
            "open": 172,
            "high": 173,
            "low": 171,
            "close": 172.5,
            "volume": 200,
            "amount": 34500,
        },
        {
            "symbol": "NBIS.US",
            "market": "us",
            "bar_time_utc": "2026-07-17 19:59:00",
            "open": 177,
            "high": 178,
            "low": 176,
            "close": 177.5,
            "volume": 300,
            "amount": 53250,
        },
    ])
    provider = ClickHouseProvider(query_fn=query)

    frame = provider.get_minute(
        ["NBIS.US"],
        datetime(2026, 7, 17, 9, 25),
        datetime(2026, 7, 17, 15, 5),
        freq="1m",
    )

    assert frame["datetime"].to_list() == [
        datetime(2026, 7, 17, 9, 30),
        datetime(2026, 7, 17, 15, 59),
    ]
    sql = query.queries[-1]
    assert "trade_date_local >= toDate('2026-07-16')" in sql
    assert "trade_date_local <= toDate('2026-07-18')" in sql


def test_minute_bars_fall_back_for_every_missing_symbol() -> None:
    query = QueryRecorder([])
    requested: list[str] = []

    def fetch_missing(symbol: str) -> list[dict]:
        requested.append(symbol)
        if symbol == "ORCL.US":
            return [{
                "time": "2026-07-17T21:30:00",
                "open": 250,
                "high": 250,
                "low": 250,
                "close": 250,
                "volume": 100,
                "turnover": 25000,
            }]
        return [{
            "time": "2026-07-17T09:30:00",
            "open": 12,
            "high": 12,
            "low": 12,
            "close": 12,
            "volume": 200,
            "turnover": 2400,
        }]

    provider = ClickHouseProvider(query_fn=query, minute_fallback_fn=fetch_missing)

    frame = provider.get_minute(
        ["ORCL.US", "000001.SZ"],
        datetime(2026, 7, 17, 0, 0),
        datetime(2026, 7, 17, 23, 59),
        freq="1m",
    ).sort("symbol")

    assert requested == ["ORCL.US", "000001.SZ"]
    assert frame["symbol"].to_list() == ["000001.SZ", "ORCL.US"]
    assert frame["datetime"].to_list() == [
        datetime(2026, 7, 17, 9, 30),
        datetime(2026, 7, 17, 9, 30),
    ]


def test_minute_bars_do_not_fall_back_for_covered_symbol() -> None:
    query = QueryRecorder([{
        "symbol": "AAPL.US",
        "market": "us",
        "bar_time_utc": "2026-07-17 13:30:00",
        "open": 210,
        "high": 211,
        "low": 209,
        "close": 210.5,
        "volume": 100,
        "amount": 21050,
    }])
    requested: list[str] = []
    provider = ClickHouseProvider(
        query_fn=query,
        minute_fallback_fn=lambda symbol: requested.append(symbol) or [],
    )

    frame = provider.get_minute(
        ["AAPL.US"],
        datetime(2026, 7, 17, 0, 0),
        datetime(2026, 7, 17, 23, 59),
        freq="1m",
    )

    assert frame.height == 1
    assert requested == []


def test_minute_fallback_keeps_one_row_per_symbol_and_timestamp() -> None:
    provider = ClickHouseProvider(
        query_fn=QueryRecorder([]),
        minute_fallback_fn=lambda _symbol: [
            {
                "time": "2026-07-17T09:30:00",
                "close": 12,
            },
            {
                "time": "2026-07-17T09:30:00",
                "close": 13,
            },
        ],
    )

    frame = provider.get_minute(
        ["000001.SZ"],
        datetime(2026, 7, 17),
        datetime(2026, 7, 17, 23, 59),
    )

    assert frame.select("symbol", "datetime", "close").to_dicts() == [{
        "symbol": "000001.SZ",
        "datetime": datetime(2026, 7, 17, 9, 30),
        "close": 13.0,
    }]


def test_strict_minute_never_uses_longbridge_fallback() -> None:
    fallback = Mock(side_effect=AssertionError("fallback forbidden"))
    provider = ClickHouseProvider(query_fn=QueryRecorder([]), minute_fallback_fn=fallback)

    frame = provider.get_minute_strict(
        ["01347.HK"],
        datetime(2026, 7, 23, 9, 30),
        datetime(2026, 7, 23, 16, 0),
    )

    assert frame.is_empty()
    assert frame.columns == ["source"]
    fallback.assert_not_called()


def test_strict_minute_marks_clickhouse_rows_as_webstock() -> None:
    query = QueryRecorder([{
        "symbol": "01347.HK",
        "market": "hk",
        "bar_time_utc": "2026-07-23 01:30:00",
        "open": 140,
        "high": 141,
        "low": 139,
        "close": 140.5,
        "volume": 100,
        "amount": 14050,
        "source_priority": 2,
    }])
    provider = ClickHouseProvider(query_fn=query)

    frame = provider.get_minute_strict(
        ["01347.HK"],
        datetime(2026, 7, 23, 9, 30),
        datetime(2026, 7, 23, 16, 0),
    )

    assert frame.select("symbol", "datetime", "source").to_dicts() == [{
        "symbol": "01347.HK",
        "datetime": datetime(2026, 7, 23, 9, 30),
        "source": "webstock",
    }]


def test_strict_minute_prefers_websocket_candlestick_for_same_minute() -> None:
    query = QueryRecorder([
        {
            "symbol": "1347.HK",
            "market": "hk",
            "bar_time_utc": "2026-07-23 01:30:00",
            "open": 140,
            "high": 141,
            "low": 139,
            "close": 140.5,
            "volume": 100,
            "amount": 14050,
            "source_priority": 2,
        },
        {
            "symbol": "1347.HK",
            "market": "hk",
            "bar_time_utc": "2026-07-23 01:30:00",
            "open": 141,
            "high": 143,
            "low": 140,
            "close": 142.5,
            "volume": 120,
            "amount": 17100,
            "source_priority": 3,
        },
    ])
    provider = ClickHouseProvider(query_fn=query)

    frame = provider.get_minute_strict(
        ["01347.HK"],
        datetime(2026, 7, 23, 9, 30),
        datetime(2026, 7, 23, 16, 0),
    )

    assert frame.select("symbol", "datetime", "close").to_dicts() == [{
        "symbol": "01347.HK",
        "datetime": datetime(2026, 7, 23, 9, 30),
        "close": 142.5,
    }]
    assert "lb_realtime_candlesticks" in query.queries[-1]
    assert "period = 'min_1'" in query.queries[-1]
    assert "3 AS source_priority" in query.queries[-1]


def test_strict_minute_maps_unpadded_webstock_hk_symbol_to_requested_symbol() -> None:
    query = QueryRecorder([{
        "symbol": "1347.HK",
        "market": "hk",
        "bar_time_utc": "2026-07-23 01:30:00",
        "open": 140,
        "high": 141,
        "low": 139,
        "close": 140.5,
        "volume": 100,
        "amount": 14050,
        "source_priority": 2,
        "cumulative_snapshot": 0,
    }])
    provider = ClickHouseProvider(query_fn=query)

    frame = provider.get_minute_strict(
        ["01347.HK"],
        datetime(2026, 7, 23, 9, 30),
        datetime(2026, 7, 23, 16, 0),
    )

    assert frame.get_column("symbol").to_list() == ["01347.HK"]
    assert query.queries[-1].count("symbol IN ('01347.HK', '1347.HK')") == 3


def test_strict_realtime_uses_only_clickhouse_query(monkeypatch) -> None:
    query = QueryRecorder([{
        "symbol": "01347.HK",
        "market": "hk",
        "snapshot_minute": "2026-07-23 10:01:00",
        "last_done": 140.5,
        "prev_close": 139,
        "open": 140,
        "high": 141,
        "low": 139,
        "change_value": 1.5,
        "change_percentage": 1.0791,
        "volume": 100,
        "turnover": 14050,
    }])
    monkeypatch.setattr(
        provider_module.httpx,
        "get",
        Mock(side_effect=AssertionError("Longbridge HTTP forbidden")),
    )
    provider = ClickHouseProvider(query_fn=query)

    rows = provider.get_realtime_strict(["01347.HK"])

    assert [row["symbol"] for row in rows] == ["01347.HK"]
    assert "symbol IN ('01347.HK', '1347.HK')" in query.queries[-1]
    assert "toStartOfDay(now('Asia/Shanghai'))" not in query.queries[-1]


def test_strict_realtime_maps_unpadded_webstock_hk_symbol_to_requested_symbol() -> None:
    query = QueryRecorder([{
        "symbol": "1347.HK",
        "market": "hk",
        "snapshot_minute": "2026-07-23 10:01:00",
        "last_done": 140.5,
        "prev_close": 139,
        "open": 140,
        "high": 141,
        "low": 139,
        "change_value": 1.5,
        "change_percentage": 1.0791,
        "volume": 100,
        "turnover": 14050,
    }])
    provider = ClickHouseProvider(query_fn=query)

    rows = provider.get_realtime_strict(["01347.HK"])

    assert [row["symbol"] for row in rows] == ["01347.HK"]


def test_strict_realtime_recomputes_change_from_latest_non_null_session_prev_close() -> None:
    query = QueryRecorder([{
        "symbol": "1347.HK",
        "market": "hk",
        "snapshot_minute": "2026-07-24 13:22:00",
        "last_done": 152,
        "prev_close": 149.4,
        "open": 146,
        "high": 154.9,
        "low": 144.7,
        "change_value": None,
        "change_percentage": 0.9894,
        "volume": 100,
        "turnover": 15200,
    }])
    provider = ClickHouseProvider(query_fn=query)

    rows = provider.get_realtime_strict(["01347.HK"])

    assert rows[0]["change_amount"] == pytest.approx(2.6)
    assert rows[0]["change_pct"] == pytest.approx((152 - 149.4) / 149.4)
    sql = query.queries[-1]
    assert "session_baselines AS" in sql
    assert "source.prev_close IS NOT NULL" in sql
    assert "toDate(source.snapshot_minute) = toDate(quote.snapshot_minute)" in sql
    assert "source.snapshot_minute <= quote.snapshot_minute" in sql
    assert "quote.symbol AS symbol" in sql


def test_strict_realtime_empty_monitor_list_does_not_query_all_symbols() -> None:
    query = QueryRecorder([])
    provider = ClickHouseProvider(query_fn=query)

    assert provider.get_realtime_strict([]) == []
    assert query.queries == []


def test_strict_realtime_keeps_fresh_us_quote_across_shanghai_midnight() -> None:
    row = {
        "symbol": "AAPL.US",
        "market": "us",
        "snapshot_minute": "2026-07-23 23:59:00",
        "last_done": 215,
        "prev_close": 210,
        "open": 211,
        "high": 216,
        "low": 209,
        "change_value": 5,
        "change_percentage": 2.3809,
        "volume": 100,
        "turnover": 21500,
    }
    queries: list[str] = []

    def query(sql: str) -> list[dict]:
        queries.append(sql)
        return [] if "toStartOfDay(now('Asia/Shanghai'))" in sql else [row]

    provider = ClickHouseProvider(query_fn=query)

    strict_rows = provider.get_realtime_strict(["AAPL.US"])
    legacy_rows = provider.get_realtime(symbols=["AAPL.US"])

    expected = datetime(2026, 7, 23, 15, 59, tzinfo=UTC)
    shanghai_now = datetime(2026, 7, 24, 0, 0, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    strict_quote_time = datetime.fromtimestamp(strict_rows[0]["timestamp"] / 1000, tz=UTC)
    assert strict_rows[0]["timestamp"] == int(expected.timestamp() * 1000)
    assert shanghai_now.astimezone(UTC) - strict_quote_time == timedelta(seconds=80)
    assert "snapshot_minute >= now('Asia/Shanghai') - INTERVAL 1 DAY" in queries[0]
    assert "snapshot_minute <= now('Asia/Shanghai')" in queries[0]
    assert "toStartOfDay(now('Asia/Shanghai'))" not in queries[0]
    assert "WHERE symbol IN ('AAPL.US')" in queries[0]
    assert legacy_rows == []
    assert "snapshot_minute >= toStartOfDay(now('Asia/Shanghai'))" in queries[1]
    assert (
        "snapshot_minute < toStartOfDay(now('Asia/Shanghai')) + INTERVAL 1 DAY"
        in queries[1]
    )
    assert "toStartOfDay(now('Asia/Shanghai'))" in queries[1]


def test_instruments_cover_all_three_markets() -> None:
    query = QueryRecorder([
        {"symbol": "000001.SZ", "market": "cn", "name": "Ping An Bank", "currency": "CNY", "lot_size": 100},
        {"symbol": "1.HK", "market": "hk", "name": "CKH HOLDINGS", "currency": "HKD", "lot_size": 500},
        {"symbol": "A.US", "market": "us", "name": "AGILENT", "currency": "USD", "lot_size": 1},
    ])
    provider = ClickHouseProvider(query_fn=query)

    rows = provider.get_instruments()

    assert [row["exchange"] for row in rows] == ["SZ", "HK", "US"]
    assert [row["market"] for row in rows] == ["cn", "hk", "us"]
    assert [row["name"] for row in rows] == ["Ping An Bank", "CKH HOLDINGS", "AGILENT"]
    assert [row["currency"] for row in rows] == ["CNY", "HKD", "USD"]
    assert [row["lot_size"] for row in rows] == [100, 500, 1]
    assert "FROM longbridge.lb_daily_bars" in query.queries[-1]
    assert "FROM longbridge.lb_symbols" in query.queries[-1]


def test_symbol_values_are_sql_escaped() -> None:
    query = QueryRecorder([])
    provider = ClickHouseProvider(query_fn=query)

    provider.get_daily(["A'B.US"], None, None)

    assert "'A''B.US'" in query.queries[-1]


def test_provider_honors_configured_database(monkeypatch) -> None:
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "market_data")
    query = QueryRecorder([])
    provider = ClickHouseProvider(query_fn=query)

    provider.get_realtime(symbols=["A.US"])

    assert "FROM market_data.lb_realtime_quotes" in query.queries[-1]


def test_daily_chunks_large_three_market_universe() -> None:
    query = QueryRecorder([])
    progress: list[tuple[int, int]] = []
    provider = ClickHouseProvider(query_fn=query)

    provider.get_daily(
        [f"SYM{i}.US" for i in range(501)],
        None,
        None,
        on_chunk_done=lambda current, total: progress.append((current, total)),
    )

    assert len(query.queries) == 2
    assert progress == [(1, 2), (2, 2)]


def test_hk_market_industries_use_full_f10_classification_with_leader_marker() -> None:
    query = QueryRecorder([
        {
            "as_of": "2026-06-25 02:54:33.613",
            "symbol": "700.HK",
            "name": "TENCENT",
            "industry": "软件服务",
            "is_leader": 1,
        },
        {
            "as_of": "2026-06-25 02:54:33.613",
            "symbol": "AAPL.US",
            "name": "APPLE",
            "industry": "消费电子",
            "is_leader": 0,
        },
    ])
    provider = ClickHouseProvider(query_fn=query)

    result = provider.get_market_industries("hk")

    assert result["market"] == "hk"
    assert result["as_of"] == "2026-06-25 02:54:33.613"
    assert result["source"] == "lb_eastmoney_f10_profiles"
    assert result["leader_source"] == "lb_company_background_industry_leaders"
    assert result["rows"] == [{
        "symbol": "700.HK",
        "name": "TENCENT",
        "main_sector": "",
        "sub_industry": "软件服务",
        "industry": "软件服务",
        "is_leader": True,
    }]
    assert "lb_eastmoney_f10_profiles" in query.queries[-1]
    assert "lb_company_background_industry_leaders" in query.queries[-1]
    assert "max(snapshot_date)" in query.queries[-1]


def test_us_market_industries_use_full_f10_classification_with_leader_marker() -> None:
    query = QueryRecorder([
        {
            "as_of": "2026-06-25 02:54:33.613",
            "symbol": "AAPL.US",
            "name": "APPLE",
            "industry": "消费电子",
            "is_leader": 1,
        }
    ])
    provider = ClickHouseProvider(query_fn=query)

    result = provider.get_market_industries("us")

    assert result["market"] == "us"
    assert result["source"] == "lb_eastmoney_f10_profiles"
    assert result["leader_source"] == "lb_sector_leader_snapshots"
    assert result["rows"][0]["industry"] == "消费电子"
    assert result["rows"][0]["is_leader"] is True
    assert "lb_eastmoney_f10_profiles" in query.queries[-1]
    assert "lb_sector_leader_snapshots" in query.queries[-1]
    assert "max(trade_date)" in query.queries[-1]


def test_hk_market_concepts_use_recent_event_themes_and_normalized_symbols() -> None:
    query = QueryRecorder([
        {
            "as_of": "2026-07-17",
            "symbol": "700.HK",
            "name": "TENCENT",
            "concept": "云计算",
        },
        {
            "as_of": "2026-07-17",
            "symbol": "AAPL.US",
            "name": "APPLE",
            "concept": "消费电子",
        },
    ])
    provider = ClickHouseProvider(query_fn=query)

    result = provider.get_market_concepts("hk")

    assert result["market"] == "hk"
    assert result["as_of"] == "2026-07-17"
    assert result["source"] == "lb_sentiment_impact_events"
    assert result["window_days"] == 30
    assert result["rows"] == [{
        "symbol": "700.HK",
        "name": "TENCENT",
        "concept": "云计算",
    }]
    sql = query.queries[-1]
    assert "lb_sentiment_impact_events" in sql
    assert "arrayJoin(affected_symbols)" in sql
    assert "arrayJoin(affected_sectors)" in sql
    assert "toUInt32OrZero" in sql
    assert "max(analysis_date) - 29" in sql
    assert "lb_daily_bars" in sql


def test_us_market_concepts_normalize_tickers_and_reject_other_market_rows() -> None:
    query = QueryRecorder([
        {
            "as_of": "2026-07-18",
            "symbol": "BRK-B.US",
            "name": "BERKSHIRE",
            "concept": "保险",
        },
        {
            "as_of": "2026-07-18",
            "symbol": "AIG.US",
            "name": "AIG",
            "concept": "保险",
        },
        {
            "as_of": "2026-07-18",
            "symbol": "700.HK",
            "name": "TENCENT",
            "concept": "互联网平台",
        },
        {
            "as_of": "2026-07-18",
            "symbol": "NBIS.US",
            "name": "Nebius",
            "concept": "美股中概/欧洲科技股",
        },
    ])
    provider = ClickHouseProvider(query_fn=query)

    result = provider.get_market_concepts("us")

    assert result["market"] == "us"
    assert result["rows"] == [
        {
            "symbol": "BRK-B.US",
            "name": "BERKSHIRE",
            "concept": "保险",
        },
        {
            "symbol": "AIG.US",
            "name": "AIG",
            "concept": "保险",
        },
    ]
    sql = query.queries[-1]
    assert "replaceAll" in sql
    assert "'.US'" in sql
    assert "count() AS support_count" in sql
    assert "row_number() OVER" in sql
    assert "theme_rank <= 20" in sql
    assert "positionCaseInsensitiveUTF8(concept, '中概') = 0" in sql
    assert "AS last_analysis_date" in sql
    assert "max(pairs.last_analysis_date)" in sql


def test_us_market_concepts_merge_market_prefixes_and_drop_singletons() -> None:
    query = QueryRecorder([
        {
            "as_of": "2026-07-18",
            "symbol": "CRWV.US",
            "name": "CoreWeave",
            "concept": "AI基础设施",
        },
        {
            "as_of": "2026-07-18",
            "symbol": "NBIS.US",
            "name": "Nebius",
            "concept": "美股AI基础设施",
        },
        {
            "as_of": "2026-07-18",
            "symbol": "NBIS.US",
            "name": "Nebius",
            "concept": "内部人交易概念",
        },
    ])
    provider = ClickHouseProvider(query_fn=query)

    result = provider.get_market_concepts("us")

    assert result["rows"] == [
        {
            "symbol": "CRWV.US",
            "name": "CoreWeave",
            "concept": "AI基础设施",
        },
        {
            "symbol": "NBIS.US",
            "name": "Nebius",
            "concept": "AI基础设施",
        },
    ]


def test_cn_market_concepts_keep_using_configured_extension_data() -> None:
    query = QueryRecorder([])
    provider = ClickHouseProvider(query_fn=query)

    result = provider.get_market_concepts("cn")

    assert result == {
        "market": "cn",
        "as_of": None,
        "source": None,
        "window_days": 30,
        "rows": [],
    }
    assert query.queries == []
