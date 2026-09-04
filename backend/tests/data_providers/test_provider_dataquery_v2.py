"""FQuantProvider × dataquery v2 wiring tests: injected client, zero network.

Covers: canonical cache-id construction, wide/xdxr/minutes/trans v2 branch
selection, legacy chain preservation when the client is disabled, blocked
bulk/range paths (engine #9/#11), moneyflow point mapping honesty, and
freshness via v2 status coverage.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import polars as pl
import pytest

from app.data_providers.fquant.dataquery_client import (
    DataQueryBlockedError,
    DataQueryClient,
    DataQueryError,
    DataQueryResult,
    DataVersion,
)
from app.data_providers.fquant.symbols import symbol_to_cache_id
from app.data_providers.fquant_provider import FQuantProvider


def version(schema: str = "legacy_csv/v1") -> DataVersion:
    return DataVersion(
        backend="legacy_raw",
        generation="",
        schema_version=schema,
        checksum="",
        source_watermark="20260903T000000+0000:1",
        coverage="2026-09-02",
        stage="legacy",
        reconciled=False,
        degraded=True,
        freshness="",
    )


class FakeDataQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows_by_dataset: dict[str, list[dict[str, Any]]] = {}
        self.status_payload: dict[str, Any] = {"status": "ok", "routes": []}

    def series(self, dataset, cache_id, **kwargs):
        self.calls.append(("series:" + dataset, {"cache_id": cache_id, **kwargs}))
        return DataQueryResult(
            f"tdx_{dataset}/a",
            tuple(self.rows_by_dataset.get(dataset, [])),
            version(),
        )

    def daily_moneyflow_point(self, cache_id, date_str):
        self.calls.append(("moneyflow:daily", {"cache_id": cache_id, "date": date_str}))
        return DataQueryResult(
            "tdx_moneyflow/a",
            (
                {
                    "trade_date": date_str,
                    "total_amount": 100.0,
                    "inflow_amount": 70.0,
                    "outflow_amount": 30.0,
                    "net_amount": 40.0,
                },
            ),
            version("tdx_moneyflow/v1"),
        )

    def minute_moneyflow_point(self, cache_id, date_str):
        self.calls.append(("moneyflow:minute", {"cache_id": cache_id, "date": date_str}))
        return DataQueryResult(
            "tdx_moneyflow_minute/a",
            ({"trade_date": date_str, "bucket_time": "09:31", "net_amount": 1.0},),
            version("tdx_moneyflow_minute/v1"),
        )

    def status(self):
        self.calls.append(("status", {}))
        return self.status_payload

    def observed_versions(self):
        return {}


def provider_with(fake: FakeDataQuery) -> FQuantProvider:
    provider = object.__new__(FQuantProvider)
    provider.name = "fquant"
    provider._dataquery = fake
    return provider


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_fund_range(self, code, start, end):
        self.calls.append(("fund_range", (code, start, end), {}))
        return pl.DataFrame({"date": [start], "main_net_inflow": [1.0]})

    def get_moneyflow_stock(self, symbol, start, end, freq="daily"):
        self.calls.append(("moneyflow_stock", (symbol, start, end), {"freq": freq}))
        return pl.DataFrame({"symbol": [symbol], "trade_date": [start]})

    def get_trans(self, code, date_str, limit=5000, asset_type=None):
        self.calls.append(("trans", (code, date_str, limit), {"asset_type": asset_type}))
        return [{"time": "09:30:03", "price": 10.0, "volume": 1, "amount": 10}]

    def get_xdxr(self, code, limit=100, asset_type=None):
        self.calls.append(("xdxr", (code,), {"limit": limit, "asset_type": asset_type}))
        return [{"date": "2024-06-18", "fenhong": 1.0}]


def provider_with_engine(fake: FakeDataQuery, engine: FakeEngine) -> FQuantProvider:
    provider = provider_with(fake)
    provider._engine = engine
    return provider


def legacy_provider() -> FQuantProvider:
    provider = object.__new__(FQuantProvider)
    provider.name = "fquant"
    provider._dataquery = None
    return provider


# --- cache ids -----------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600519.SH", "sh600519"),
        ("600519", "sh600519"),
        ("000001.SZ", "sz000001"),
        ("300750.SZ", "sz300750"),
        ("832000.BJ", "bj832000"),
        ("430047.BJ", "bj430047"),
        ("510300.SH", "sh510300"),
    ],
)
def test_symbol_to_cache_id_follows_engine_prefix_rule(symbol, expected):
    assert symbol_to_cache_id(symbol) == expected


def test_symbol_to_cache_id_rejects_non_a_share():
    assert symbol_to_cache_id("00700.HK") is None
    assert symbol_to_cache_id("") is None
    assert symbol_to_cache_id("ABC") is None


def test_query_series_rejects_symbols_without_cache_id():
    provider = provider_with(FakeDataQuery())
    with pytest.raises(DataQueryError) as exc:
        provider._query_series("wide", "00700.HK")
    assert exc.value.code == "invalid_query"


def test_query_series_builds_canonical_cache_id():
    fake = FakeDataQuery()
    provider = provider_with(fake)
    provider._query_series("wide", "600519.SH")
    assert fake.calls == [
        (
            "series:wide",
            {
                "cache_id": "sh600519",
                "date": None,
                "start": None,
                "end": None,
                "limit": 2500,
                "tail": False,
            },
        )
    ]


# --- wide / xdxr v2 branches ---------------------------------------------


def _bare(provider: FQuantProvider) -> None:
    # legacy-chain attributes referenced by raw reconstruction
    provider._fstore = type(
        "FStore",
        (),
        {
            "query": staticmethod(
                lambda sql, params=None: [
                    {
                        "date": "2026-09-01",
                        "oracle_open": 1.0,
                        "oracle_high": 2.0,
                        "oracle_low": 0.5,
                        "oracle_close": 1.5,
                        "oracle_volume": 100.0,
                        "oracle_amount": 150.0,
                    }
                ]
            )
        },
    )()
    provider._engine = type(
        "Engine",
        (),
        {
            "get_xdxr": staticmethod(lambda *a, **k: []),
            "get_wide": staticmethod(lambda *a, **k: []),
        },
    )()


def test_get_daily_stock_uses_v2_wide_and_reconstructs_raw():
    fake = FakeDataQuery()
    fake.rows_by_dataset["wide"] = [
        {
            "date": "20260901",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100,
            "amount": 150,
            "last_close": 1.4,
            "change_rate": 7.14,
        }
    ]
    fake.rows_by_dataset["xdxr"] = []
    provider = provider_with(fake)
    _bare(provider)
    rows = provider._get_daily_from_engine_wide(
        "600519.SH",
        "600519",
        datetime(2026, 8, 1),
        datetime(2026, 9, 2),
        "stock",
    )
    assert rows and rows[0]["date"] == "2026-09-01"
    datasets = [name for name, _ in fake.calls]
    assert "series:wide" in datasets and "series:xdxr" in datasets


def test_get_daily_above_row_cap_is_blocked_not_silently_capped():
    provider = provider_with(FakeDataQuery())
    _bare(provider)
    with pytest.raises(DataQueryBlockedError):
        provider._get_daily_from_engine_wide(
            "600519.SH",
            "600519",
            datetime(2018, 1, 1),
            datetime(2026, 9, 2),
            "stock",
        )


def test_adj_events_use_v2_xdxr_for_a_share():
    fake = FakeDataQuery()
    fake.rows_by_dataset["xdxr"] = [
        {
            "date": "20240618",
            "category": 1,
            "name": "dividend",
            "fenhong": 1.0,
            "fenshu": 0.0,
            "songzhuangu": 0.0,
            "peigu": 0.0,
            "peigujia": 0.0,
        }
    ]
    provider = provider_with(fake)
    events = provider._get_adj_events_from_engine("600519.SH", "600519")
    assert events and events[0]["trade_date"] == "2024-06-18"


def test_adj_events_route_etf_to_legacy_xdxr():
    fake = FakeDataQuery()
    engine = FakeEngine()
    provider = provider_with_engine(fake, engine)
    events = provider._get_adj_events_from_engine("510300.SH", "510300")
    assert events and engine.calls[-1][0] == "xdxr"
    assert fake.calls == []


# --- minutes / trans ------------------------------------------------------


def test_get_minute_multi_day_bounded_loop_and_narrow_guard():
    fake = FakeDataQuery()
    fake.rows_by_dataset["minutes"] = [{"price": 10.0, "volume": 5}]
    provider = provider_with(fake)
    df = provider.get_minute(
        ["600519.SH"],
        datetime(2026, 9, 1, 9, 30),
        datetime(2026, 9, 2, 15, 0),
        "stock",
        "1m",
    )
    assert not df.is_empty()
    minute_calls = [c for c in fake.calls if c[0] == "series:minutes"]
    assert len(minute_calls) == 2  # one per day

    with pytest.raises(DataQueryBlockedError):
        provider.get_minute(
            ["600519.SH"],
            datetime(2026, 8, 1, 9, 30),
            datetime(2026, 9, 2, 15, 0),
            "stock",
            "1m",
        )


def test_get_minute_unbounded_request_rejected():
    provider = provider_with(FakeDataQuery())
    assert provider.get_minute(["600519.SH"], None, None, "stock").is_empty()
    assert provider.get_minute(["600519.SH"], None, None, "stock", "1m").is_empty()


def test_get_transactions_point_uses_v2_and_large_limits_use_legacy():
    fake = FakeDataQuery()
    fake.rows_by_dataset["trans"] = [
        {"time": "09:30:03", "price": 10.01, "volume": 120, "amount": 120120, "direction": 1}
    ]
    provider = provider_with(fake)
    df = provider.get_transactions("600519.SH", datetime(2026, 9, 1), limit=1000)
    assert df.height == 1
    assert fake.calls[0][1]["cache_id"] == "sh600519"

    engine = FakeEngine()
    provider = provider_with_engine(FakeDataQuery(), engine)
    provider.get_transactions("600519.SH", datetime(2026, 9, 1), limit=5000)
    assert engine.calls[-1][0] == "trans"
    assert engine.calls[-1][1][2] == 5000
    assert provider._dataquery.calls == []


# --- moneyflow ------------------------------------------------------------


def test_moneyflow_daily_point_maps_totals_without_faking_main_split():
    provider = provider_with(FakeDataQuery())
    df = provider.get_moneyflow_daily(["600519.SH"], datetime(2026, 9, 1))
    row = df.to_dicts()[0]
    assert row["total_net"] == 40.0
    # main_* must stay None: v2 exposes no main/total split
    assert row["main_net"] is None
    assert row["main_inflow"] is None


def test_moneyflow_daily_over_16_symbols_is_blocked():
    provider = provider_with(FakeDataQuery())
    symbols = [f"{600000 + i}.SH" for i in range(17)]
    with pytest.raises(DataQueryBlockedError):
        provider.get_moneyflow_daily(symbols, datetime(2026, 9, 1))


def test_moneyflow_range_routes_to_legacy_before_v2():
    fake = FakeDataQuery()
    engine = FakeEngine()
    provider = provider_with_engine(fake, engine)
    provider.get_moneyflow_range("600519.SH", datetime(2026, 8, 1), datetime(2026, 9, 1))
    assert engine.calls[-1] == ("fund_range", ("600519", "2026-08-01", "2026-09-01"), {})
    assert fake.calls == []


def test_moneyflow_stock_point_schema_and_multi_day_legacy_route():
    fake = FakeDataQuery()
    engine = FakeEngine()
    provider = provider_with_engine(fake, engine)
    day = datetime(2026, 9, 1)
    daily = provider.get_moneyflow_stock("600519.SH", day, day, freq="daily")
    row = daily.to_dicts()[0]
    for field in ("trade_date", "total_amount", "net_amount", "inflow_amount", "outflow_amount"):
        assert field in daily.columns
    assert row["trade_date"] == "2026-09-01"
    assert row["total_amount"] == 100.0
    assert row["net_amount"] == 40.0
    assert row["inflow_amount"] == 70.0
    assert row["outflow_amount"] == 30.0
    assert row["main_traditional_net"] is None

    provider.get_moneyflow_stock("600519.SH", datetime(2026, 8, 31), day, freq="daily")
    assert engine.calls[-1][0] == "moneyflow_stock"
    assert fake.calls == [("moneyflow:daily", {"cache_id": "sh600519", "date": "20260901"})]


def test_moneyflow_stock_same_day_minute_point_uses_v2():
    provider = provider_with(FakeDataQuery())
    day = datetime(2026, 9, 1)
    minute = provider.get_moneyflow_stock("600519.SH", day, day, freq="minute")
    row = minute.to_dicts()[0]
    assert row["bucket_time"] == "09:31"
    assert row["net_amount"] == 1.0
    assert row["main_traditional_net"] is None


# --- legacy chain preserved -----------------------------------------------


def test_legacy_chain_unchanged_when_client_disabled():
    provider = legacy_provider()
    provider._engine = type(
        "Engine", (), {"get_xdxr": staticmethod(lambda *a, **k: [{"date": "2024-06-18"}])}
    )()
    provider._fstore = type("FStore", (), {"query": staticmethod(lambda sql, params=None: [])})()
    events = provider._get_adj_events_from_engine("600519.SH", "600519")
    assert events and events[0]["trade_date"] == "2024-06-18"  # legacy ISO shape

    provider._engine = type(
        "Engine", (), {"get_xdxr": staticmethod(lambda *a, **k: [{"date": None}])}
    )()
    events = provider._get_adj_events_from_engine("600519.SH", "600519")
    assert len(events) == 1 and events[0]["trade_date"] is None  # legacy passthrough


def test_freshness_uses_v2_status_coverage():
    fake = FakeDataQuery()
    fake.status_payload = {
        "status": "ok",
        "routes": [
            {
                "dataset": "tdx_day/a",
                "configured": True,
                "observed": True,
                "backend": "legacy_raw",
                "schema_version": "legacy_csv/v1",
                "generation": "",
                "source_watermark": "w",
                "coverage": "2026-09-02",
                "stage": "legacy",
                "reconciled": False,
                "degraded": True,
                "error_code": "",
                "version": {},
            }
        ],
    }
    provider = provider_with(fake)
    assert provider.get_daily_freshness() == date(2026, 9, 2)


def test_freshness_degrades_to_none_on_unavailable_status():
    class Broken(FakeDataQuery):
        def status(self):
            raise DataQueryError("unavailable", message="dataquery backend is unavailable")

    provider = provider_with(Broken())
    assert provider.get_daily_freshness() is None


def test_v2_metadata_exposed_without_storage_details():
    class Versioned(FakeDataQuery):
        def observed_versions(self):
            return {"tdx_day/a": {"backend": "legacy_raw", "schema_version": "legacy_csv/v1"}}

    provider = provider_with(Versioned())
    assert provider.get_dataquery_versions()["tdx_day/a"]["backend"] == "legacy_raw"
    assert legacy_provider().get_dataquery_versions() == {}


# --- bulk fan-out guards (engine #9/#11) ---------------------------------


def _wide_provider() -> tuple[FQuantProvider, FakeDataQuery]:
    fake = FakeDataQuery()
    fake.rows_by_dataset["wide"] = []
    fake.rows_by_dataset["xdxr"] = []
    provider = provider_with(fake)
    _bare(provider)
    return provider, fake


def test_get_daily_bulk_symbols_blocked_with_zero_http_calls():
    provider, fake = _wide_provider()
    symbols = [f"{600000 + i}.SH" for i in range(17)]
    with pytest.raises(DataQueryBlockedError) as exc:
        provider.get_daily(symbols, datetime(2026, 8, 1), datetime(2026, 9, 2), "stock")
    assert exc.value.dataset == "tdx_wide/a"
    assert fake.calls == []  # guard fires before any HTTP read


def test_get_minute_bulk_symbols_blocked_with_zero_http_calls():
    provider, fake = _wide_provider()
    symbols = [f"{600000 + i}.SH" for i in range(17)]
    with pytest.raises(DataQueryBlockedError) as exc:
        provider.get_minute(
            symbols, datetime(2026, 9, 1, 9, 30), datetime(2026, 9, 1, 15, 0), "stock"
        )
    assert exc.value.dataset == "tdx_minutes/a"
    assert fake.calls == []


def test_bulk_guard_does_not_touch_index_or_legacy_batches():
    provider, fake = _wide_provider()
    symbols = [f"{i:06d}.HK" for i in range(20)]
    # HK/index batch: not v2-eligible symbols, guard must not fire (HK DuckDB path runs)
    provider.get_daily(symbols, datetime(2026, 8, 1), datetime(2026, 9, 2), "stock")
    assert fake.calls == []
