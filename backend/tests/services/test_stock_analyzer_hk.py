"""HK stock analysis falls back to local on-demand enrichment when batch data is absent."""
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
