from types import SimpleNamespace

import polars as pl

from app.api.kline import search_instruments
from app.services.instrument_sync import _flatten_instruments, _preserve_existing_names


def test_provider_instruments_keep_market_metadata() -> None:
    rows = _flatten_instruments([
        {
            "symbol": "1.HK",
            "name": "CKH HOLDINGS",
            "code": "1",
            "exchange": "HK",
            "market": "hk",
            "currency": "HKD",
            "lot_size": 500,
            "asset_type": "stock",
        }
    ])

    assert rows == [{
        "symbol": "1.HK",
        "name": "CKH HOLDINGS",
        "code": "1",
        "exchange": "HK",
        "region": None,
        "type": "stock",
        "market": "hk",
        "currency": "HKD",
        "lot_size": 500,
        "listing_date": None,
        "total_shares": None,
        "float_shares": None,
        "tick_size": None,
        "limit_up": None,
        "limit_down": None,
    }]


def test_existing_instrument_names_are_preserved(tmp_path) -> None:
    path = tmp_path / "instruments.parquet"
    pl.DataFrame([
        {"symbol": "000001.SZ", "name": "平安银行"},
    ]).write_parquet(path)
    incoming = pl.DataFrame([
        {"symbol": "000001.SZ", "name": "PAB", "market": "cn"},
        {"symbol": "1.HK", "name": "CKH HOLDINGS", "market": "hk"},
    ])

    merged = _preserve_existing_names(incoming, path)

    assert merged["name"].to_list() == ["平安银行", "CKH HOLDINGS"]
    assert merged["market"].to_list() == ["cn", "hk"]


class _Repo:
    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        assert asset_type == "stock"
        return pl.DataFrame([
            {"symbol": "000001.SZ", "name": "平安银行", "code": "000001", "market": "cn"},
            {"symbol": "1.HK", "name": "CKH HOLDINGS", "code": "1", "market": "hk"},
            {"symbol": "A.US", "name": "AGILENT", "code": "A", "market": "us"},
        ])


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_Repo())))


def test_instrument_search_returns_market_identity() -> None:
    result = search_instruments(
        _request(),
        q=".",
        limit=20,
        asset_types="stock",
        markets="",
    )

    assert {row["market"] for row in result["results"]} == {"cn", "hk", "us"}


def test_instrument_search_filters_requested_market() -> None:
    result = search_instruments(
        _request(),
        q=".",
        limit=20,
        asset_types="stock",
        markets="hk",
    )

    assert [row["symbol"] for row in result["results"]] == ["1.HK"]
