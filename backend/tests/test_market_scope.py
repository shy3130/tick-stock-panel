from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.services.market_scope import (
    filter_frame_by_market,
    market_cache_key,
    market_currency,
    market_latest_date,
    symbols_for_market,
    normalize_market,
)


def _mixed_market_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ", "430017.BJ", "00700.HK", "AAPL.US"],
            "close": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )


def test_normalize_market_and_currency_contract() -> None:
    assert normalize_market("HK") == "hk"
    assert normalize_market("unknown") == "cn"
    assert normalize_market(None) == "cn"
    assert market_currency("cn") == "CNY"
    assert market_currency("hk") == "HKD"
    assert market_currency("us") == "USD"


def test_filter_frame_by_market_never_leaks_other_markets() -> None:
    frame = _mixed_market_frame()

    assert filter_frame_by_market(frame, "cn").get_column("symbol").to_list() == [
        "600000.SH",
        "000001.SZ",
        "430017.BJ",
    ]
    assert filter_frame_by_market(frame, "hk").get_column("symbol").to_list() == ["00700.HK"]
    assert filter_frame_by_market(frame, "us").get_column("symbol").to_list() == ["AAPL.US"]


def test_market_cache_key_isolates_same_date_between_markets() -> None:
    target = date(2026, 7, 17)

    assert market_cache_key("cn", target) == "cn:2026-07-17"
    assert market_cache_key("hk", target) == "hk:2026-07-17"
    assert market_cache_key("us", None) == "us:latest"


def test_market_latest_date_uses_market_specific_symbols() -> None:
    class Repo:
        def __init__(self) -> None:
            self.sql = ""

        def execute_one(self, sql: str):
            self.sql = sql
            return (date(2026, 7, 17),)

    repo = Repo()

    assert market_latest_date(repo, "hk") == date(2026, 7, 17)
    assert "%.HK" in repo.sql
    assert ".SH" not in repo.sql


def test_symbols_for_market_reads_instrument_universe() -> None:
    class Repo:
        def get_instruments_asset(self, asset_type: str):
            assert asset_type == "stock"
            return _mixed_market_frame()

    assert symbols_for_market(Repo(), "us") == ["AAPL.US"]


def test_market_overview_passes_market_into_screener(monkeypatch) -> None:
    from app.services import market_overview_builder as builder

    captured: dict[str, str] = {}

    class Screener:
        def __init__(self, repo, asset_type: str = "stock", market: str = "cn") -> None:
            captured["market"] = market

        def _load_enriched_for_date(self, target_date: date) -> pl.DataFrame:
            return pl.DataFrame()

    monkeypatch.setattr(builder, "ScreenerService", Screener)
    monkeypatch.setattr(builder, "market_latest_date", lambda repo, market: date(2026, 7, 17))

    result = builder.build_market_overview(object(), market="hk")

    assert captured["market"] == "hk"
    assert result["market"] == "hk"


def test_hk_market_overview_ranks_clickhouse_concepts_and_industries(monkeypatch) -> None:
    from app.services import market_overview_builder as builder

    class Screener:
        def __init__(self, repo, asset_type: str = "stock", market: str = "cn") -> None:
            assert market == "hk"

        def _load_enriched_for_date(self, target_date: date) -> pl.DataFrame:
            return pl.DataFrame(
                {
                    "symbol": ["700.HK", "9988.HK"],
                    "name": ["腾讯控股", "阿里巴巴-W"],
                    "close": [600.0, 160.0],
                    "change_pct": [0.02, -0.01],
                    "amount": [1_000_000.0, 2_000_000.0],
                    "volume": [1_000.0, 2_000.0],
                }
            )

    class DimensionProvider:
        def get_market_concepts(self, market: str):
            assert market == "hk"
            return {
                "rows": [
                    {"symbol": "700.HK", "concept": "港股互联网"},
                    {"symbol": "9988.HK", "concept": "港股互联网"},
                ]
            }

        def get_market_industries(self, market: str):
            assert market == "hk"
            return {
                "rows": [
                    {"symbol": "700.HK", "industry": "软件服务"},
                    {"symbol": "9988.HK", "industry": "软件服务"},
                ]
            }

    monkeypatch.setattr(builder, "ScreenerService", Screener)
    monkeypatch.setattr(builder, "market_latest_date", lambda repo, market: date(2026, 7, 17))

    result = builder.build_market_overview(
        object(),
        market="hk",
        dimension_provider=DimensionProvider(),
    )

    assert result["concept_rank"]["leading"][0]["name"] == "港股互联网"
    assert result["concept_rank"]["leading"][0]["count"] == 2
    assert result["industry_rank"]["leading"][0]["name"] == "软件服务"
    assert result["industry_rank"]["leading"][0]["count"] == 2


def test_market_industries_endpoint_delegates_selected_market(monkeypatch) -> None:
    from app.api import screener

    class Provider:
        def get_market_industries(self, market: str):
            return {"market": market, "as_of": "2026-07-16", "source": "test", "rows": []}

    monkeypatch.setattr(screener, "ClickHouseProvider", Provider, raising=False)

    result = screener.market_industries("US")

    assert result["market"] == "us"


def test_market_concepts_endpoint_delegates_selected_market(monkeypatch) -> None:
    from app.api import screener

    class Provider:
        def get_market_concepts(self, market: str):
            return {
                "market": market,
                "as_of": "2026-07-17",
                "source": "test",
                "window_days": 30,
                "rows": [],
            }

    monkeypatch.setattr(screener, "ClickHouseProvider", Provider, raising=False)

    result = screener.market_concepts("HK")

    assert result["market"] == "hk"


def test_market_snapshot_loads_selected_market_before_filtering(monkeypatch) -> None:
    from app.api import screener

    class Repo:
        def execute_one(self, sql: str):
            return (date(2026, 7, 17),)

    class Service:
        def __init__(self, repo, asset_type: str = "stock") -> None:
            self.market = "cn"

        def _load_enriched_for_date(self, target_date: date) -> pl.DataFrame:
            symbol = "700.HK" if self.market == "hk" else "600000.SH"
            return pl.DataFrame({"symbol": [symbol], "name": [symbol], "close": [1.0]})

    monkeypatch.setattr(screener, "ScreenerService", Service)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=Repo())))

    result = screener.market_snapshot(request, "hk")

    assert [row["symbol"] for row in result["rows"]] == ["700.HK"]
