"""HK daily kline endpoint labels adjustment and passes asset_type to enrichment."""
from types import SimpleNamespace

import polars as pl

import app.api.kline as kline_mod


class _Repo:
    def execute_one(self, sql, params=None):
        return None


class _Provider:
    def get_daily(self, symbols, start_time, end_time, asset_type):
        return pl.DataFrame(
            {
                "symbol": [symbols[0], symbols[0]],
                "date": [start_time.date(), end_time.date()],
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.0, 101.0],
                "volume": [1_000, 1_100],
                "amount": [100_000.0, 111_100.0],
            }
        )

    def get_adj_factors(self, symbols, start_time, end_time, asset_type):
        raise AssertionError("HK should not request adjustment factors")

    def get_instruments(self, asset_type):
        assert asset_type == "hk"
        return pl.DataFrame({
            "symbol": ["00700.HK"],
            "name": ["腾讯控股"],
            "total_shares": [10_000.0],
            "float_shares": [1_000.0],
        })


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_Repo(), quote_service=None)))


def test_local_kline_passes_asset_type_and_marks_adjustment(monkeypatch):
    provider = _Provider()
    captured = {}
    real_compute = kline_mod.compute_enriched

    def spy(raw, factors=None, asset_type="stock", **kwargs):
        captured["asset_type"] = asset_type
        return real_compute(raw, factors=factors, asset_type=asset_type, **kwargs)

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)
    monkeypatch.setattr(kline_mod, "compute_enriched", spy)

    assert kline_mod._asset_type_for_symbol("00700.HK") == "hk"
    assert kline_mod._adjustment_label("00700.HK") == "none"
    assert kline_mod._adjustment_label("600519.SH") == "xdxr"

    resp = kline_mod.get_daily(
        _request(),
        "00700.HK",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert captured["asset_type"] == "hk"
    assert resp["adjustment"] == "none"
    assert resp["source"] == "local_disk"
    assert resp["stock_info"]["float_shares"] == 1_000.0
    assert resp["rows"][0]["turnover_rate"] == 100.0
