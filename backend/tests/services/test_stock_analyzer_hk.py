"""HK stock analysis falls back to local on-demand enrichment when batch data is absent."""
from datetime import date

import polars as pl

import app.services.stock_analyzer as sa


class _EmptyRepo:
    def get_daily(self, symbol, start, end):
        return pl.DataFrame()


def test_hk_falls_back_to_local_on_demand(monkeypatch):
    called = {}

    def fake_local(symbol, start, end):
        called["symbol"] = symbol
        return pl.DataFrame(
            {
                "symbol": [symbol],
                "date": ["2026-07-01"],
                "close": [431.2],
                "ma5": [430.0],
            }
        )

    monkeypatch.setattr(sa, "_load_kline_local_on_demand", fake_local)
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    df = sa._load_kline(_EmptyRepo(), "00700.HK")

    assert not df.is_empty()
    assert called["symbol"] == "00700.HK"


def test_a_share_uses_batch_table_first(monkeypatch):
    class _Repo:
        def get_daily(self, symbol, start, end):
            return pl.DataFrame({"symbol": [symbol], "date": ["2026-07-01"], "close": [1.0]})

    monkeypatch.setattr(
        sa,
        "_load_kline_local_on_demand",
        lambda *args: (_ for _ in ()).throw(AssertionError("should not use fallback")),
    )

    assert not sa._load_kline(_Repo(), "600519.SH").is_empty()


def test_a_share_local_on_demand_uses_provider_float_shares(monkeypatch):
    class _Provider:
        def get_daily(self, symbols, start, end, asset_type):
            return pl.DataFrame({
                "symbol": [symbols[0]],
                "date": [date(2026, 7, 1)],
                "open": [10.0],
                "high": [10.0],
                "low": [10.0],
                "close": [10.0],
                "volume": [100.0],
                "amount": [1000.0],
            })

        def get_instruments(self, asset_type):
            return pl.DataFrame({
                "symbol": ["600519.SH"],
                "name": ["贵州茅台"],
                "float_shares": [1_000.0],
            })

        def get_adj_factors(self, symbols, start, end, asset_type):
            return pl.DataFrame()

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: _Provider())

    out = sa._load_kline_local_on_demand("600519.SH", date(2026, 7, 1), date(2026, 7, 1))

    assert out["turnover_rate"].item() == 10.0
