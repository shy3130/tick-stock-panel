from datetime import UTC, datetime

from app.plugins.clickhouse.provider import ClickHouseProvider


class QueryRecorder:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def __call__(self, sql: str) -> list[dict]:
        self.queries.append(sql)
        return self.rows


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
