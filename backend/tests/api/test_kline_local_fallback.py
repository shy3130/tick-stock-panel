from datetime import date, datetime
from types import SimpleNamespace

import polars as pl

from app.api import kline


class FakeRepo:
    def __init__(self):
        self.daily_calls = 0
        self.batch_calls = 0

    def get_daily(self, symbol, start, end):
        self.daily_calls += 1
        return pl.DataFrame()

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.batch_calls += 1
        return pl.DataFrame()

    def execute_one(self, sql, params=None):
        return None


class FakeProvider:
    def __init__(self):
        self.daily_args = None
        self.daily_calls = []
        self.adj_args = None

    def get_daily(self, symbols, start_time, end_time, asset_type):
        self.daily_args = (symbols, start_time, end_time, asset_type)
        self.daily_calls.append((symbols, start_time, end_time, asset_type))
        return pl.DataFrame({
            "symbol": [symbols[0]],
            "date": [start_time.date()],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [100.0],
            "amount": [100.0],
        })

    def get_adj_factors(self, symbols, start_time, end_time, asset_type):
        self.adj_args = (symbols, start_time, end_time, asset_type)
        return pl.DataFrame()

    def get_instruments(self, asset_type):
        rows = {
            "stock": ("600519.SH", "贵州茅台", "600519", "SH", 2_000.0, 1_000.0),
            "hk": ("02577.HK", "英诺赛科", "02577", "HK", 2_000.0, 1_000.0),
            "etf": ("513050.SH", "中概互联", "513050", "SH", 2_000.0, 1_000.0),
        }
        symbol, name, code, exchange, total_shares, float_shares = rows.get(
            asset_type,
            ("000001.INDEX", "指数", "000001", "INDEX", None, None),
        )
        return pl.DataFrame({
            "symbol": [symbol],
            "name": [name],
            "code": [code],
            "exchange": [exchange],
            "asset_type": [asset_type],
            "source": ["fake"],
            "total_shares": [total_shares],
            "float_shares": [float_shares],
        })


class CachedRepo(FakeRepo):
    def get_daily(self, symbol, start, end):
        self.daily_calls += 1
        return pl.DataFrame({
            "symbol": [symbol],
            "date": [start],
            "open": [9.0],
            "high": [9.0],
            "low": [9.0],
            "close": [9.0],
            "volume": [9.0],
        })

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.batch_calls += 1
        return pl.DataFrame({
            "symbol": [symbols[0]],
            "date": [start],
            "open": [9.0],
            "high": [9.0],
            "low": [9.0],
            "close": [9.0],
            "volume": [9.0],
        })


def request(repo=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo or FakeRepo(), quote_service=None)))


def test_daily_local_fallback_passes_datetime_to_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["source"] == "local_disk"
    assert isinstance(provider.daily_args[1], datetime)
    assert isinstance(provider.daily_args[2], datetime)
    assert provider.daily_args[3] == "stock"
    assert isinstance(provider.adj_args[1], datetime)
    assert isinstance(provider.adj_args[2], datetime)
    assert provider.adj_args[3] == "stock"
    assert resp["stock_info"]["float_shares"] == 1_000.0
    assert resp["rows"][0]["turnover_rate"] == 10.0


def test_daily_local_fallback_passes_hk_asset_type_and_skips_adj(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "02577.HK",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["source"] == "local_disk"
    assert provider.daily_args[0] == ["02577.HK"]
    assert provider.daily_args[3] == "hk"
    assert provider.adj_args is None
    assert resp["stock_info"]["float_shares"] == 1_000.0
    assert resp["rows"][0]["turnover_rate"] == 10.0


def test_daily_batch_local_fallback_passes_datetime_to_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(), {"symbols": ["600519.SH"], "days": 5})

    assert "600519.SH" in resp["data"]
    assert isinstance(provider.daily_args[1], datetime)
    assert isinstance(provider.daily_args[2], datetime)
    assert provider.daily_args[3] == "stock"


def test_daily_batch_local_fallback_splits_asset_types(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(), {"symbols": ["600519.SH", "513050.SH", "02577.HK"], "days": 5})

    assert set(resp["data"]) == {"600519.SH", "513050.SH", "02577.HK"}
    assert [(args[0], args[3]) for args in provider.daily_calls] == [
        (["600519.SH"], "stock"),
        (["513050.SH"], "etf"),
        (["02577.HK"], "hk"),
    ]


def test_daily_local_mode_ignores_cached_raw(monkeypatch):
    provider = FakeProvider()
    repo = CachedRepo()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(repo),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert repo.daily_calls == 0
    assert resp["source"] == "local_disk"
    assert resp["rows"][0]["close"] == 1.0


def test_live_candle_overwrites_derived_change_fields():
    class _QuoteService:
        def get_enriched_today(self):
            return (
                pl.DataFrame({
                    "symbol": ["600519.SH"],
                    "date": [date.today()],
                    "open": [11.0],
                    "high": [12.0],
                    "low": [10.5],
                    "close": [11.5],
                    "volume": [123.0],
                    "amount": [1400.0],
                    "change_pct": [0.15],
                    "change_amount": [1.5],
                    "amplitude": [0.15],
                    "turnover_rate": [2.3],
                }),
                date.today(),
            )

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=_QuoteService())))
    rows = [{
        "date": str(date.today()),
        "symbol": "600519.SH",
        "close": 10.0,
        "change_pct": 0.01,
        "change_amount": 0.1,
        "amplitude": 0.04,
        "turnover_rate": 1.0,
    }]

    out = kline._maybe_inject_live_candle(req, "600519.SH", rows)

    assert out[0]["close"] == 11.5
    assert out[0]["change_pct"] == 0.15
    assert out[0]["change_amount"] == 1.5
    assert out[0]["amplitude"] == 0.15
    assert out[0]["turnover_rate"] == 2.3


class FakeProviderWithMoneyflow(FakeProvider):
    def __init__(self, moneyflow_df):
        super().__init__()
        self._moneyflow_df = moneyflow_df
        self.moneyflow_args = None

    def get_moneyflow_range(self, symbol, start, end):
        self.moneyflow_args = (symbol, start, end)
        return self._moneyflow_df


def test_daily_local_mode_merges_main_net_inflow_for_stock(monkeypatch):
    moneyflow_df = pl.DataFrame({
        "date": ["2026-07-01"],
        "main_net_inflow": [300.0],
    })
    provider = FakeProviderWithMoneyflow(moneyflow_df)
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert provider.moneyflow_args is not None
    assert provider.moneyflow_args[0] == "600519.SH"
    assert resp["rows"][0]["main_net_inflow"] == 300.0


def test_daily_local_mode_skips_moneyflow_for_non_stock(monkeypatch):
    provider = FakeProviderWithMoneyflow(None)

    def _fail_if_called(symbol, start, end):
        raise AssertionError("get_moneyflow_range should not be called for non-stock asset types")

    provider.get_moneyflow_range = _fail_if_called

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "02577.HK",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None


def test_daily_local_mode_main_net_inflow_null_when_moneyflow_empty(monkeypatch):
    provider = FakeProviderWithMoneyflow(pl.DataFrame())
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None


def test_daily_local_mode_moneyflow_exception_does_not_500(monkeypatch):
    provider = FakeProvider()

    def _raise(symbol, start, end):
        raise RuntimeError("disk read failed")

    provider.get_moneyflow_range = _raise

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None
    assert resp["rows"][0]["close"] == 1.0


def test_daily_batch_local_mode_ignores_cached_raw(monkeypatch):
    provider = FakeProvider()
    repo = CachedRepo()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(repo), {"symbols": ["600519.SH"], "days": 5})

    assert repo.batch_calls == 0
    assert resp["data"]["600519.SH"][0]["close"] == 1.0
