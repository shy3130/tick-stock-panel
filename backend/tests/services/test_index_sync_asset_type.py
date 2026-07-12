from datetime import datetime

import polars as pl

from app.services import index_sync
from app.capabilities import Cap, CapabilityLimits, CapabilitySet


class FakeProvider:
    def __init__(self, instruments: pl.DataFrame | None = None):
        self.calls = []
        self._instruments = instruments

    def get_daily(self, symbols, start_time, end_time, asset_type):
        self.calls.append((tuple(symbols), asset_type))
        return pl.DataFrame({
            "symbol": symbols,
            "date": [start_time.date()] * len(symbols),
            "open": [1.0] * len(symbols),
            "high": [1.0] * len(symbols),
            "low": [1.0] * len(symbols),
            "close": [1.0] * len(symbols),
            "volume": [1_000.0] * len(symbols),
            "amount": [1.0] * len(symbols),
        })

    def get_instruments(self, asset_type):
        return self._instruments if self._instruments is not None else pl.DataFrame()


class FakeRepo:
    def __init__(self, hk_instruments: pl.DataFrame | None = None):
        self.index_daily = []
        self.etf_daily = []
        self.hk_daily = []
        self.hk_enriched = []
        self._hk_instruments = hk_instruments if hk_instruments is not None else pl.DataFrame()

    def get_hk_instruments(self):
        return self._hk_instruments

    def append_index_daily(self, df):
        self.index_daily.append(df)

    def append_index_enriched(self, df):
        pass

    def append_etf_daily(self, df):
        self.etf_daily.append(df)

    def append_etf_enriched(self, df):
        pass

    def append_hk_daily(self, df):
        self.hk_daily.append(df)

    def append_hk_enriched(self, df):
        self.hk_enriched.append(df)

    def refresh_index_views(self):
        pass


def capset():
    return CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=500)})


def test_index_daily_sync_passes_index_asset_type(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)
    monkeypatch.setattr("app.services.index_sync.compute_enriched", lambda raw, **kwargs: raw)

    repo = FakeRepo()
    rows = index_sync.sync_and_persist_index_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["000001.SH"],
    )

    assert rows == 1
    assert provider.calls == [(("000001.SH",), "index")]


def test_etf_daily_sync_passes_etf_asset_type(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)
    monkeypatch.setattr("app.services.index_sync.compute_enriched", lambda raw, **kwargs: raw)
    monkeypatch.setattr("app.services.index_sync._load_etf_factors", lambda repo: pl.DataFrame())

    repo = FakeRepo()
    rows = index_sync.sync_and_persist_etf_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["510300.ETF"],
    )

    assert rows == 1
    assert provider.calls == [(("510300.ETF",), "etf")]


def test_hk_daily_sync_passes_hk_asset_type(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)
    monkeypatch.setattr("app.services.index_sync.compute_enriched", lambda raw, **kwargs: raw)

    repo = FakeRepo()
    rows = index_sync.sync_and_persist_hk_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["00700.HK"],
    )

    assert rows == 1
    assert provider.calls == [(("00700.HK",), "hk")]
    assert len(repo.hk_daily) == 1
    assert len(repo.hk_enriched) == 1


def test_hk_instruments_sync_keeps_float_shares(monkeypatch):
    """回归测试:_fetch_instruments_by_type 原先无条件裁到 symbol/name/code 三列,
    港股 instruments 落盘后丢失 float_shares,导致 enriched 换手率永远算不出来
    (_attach_turnover_rate 要求 instruments 带 float_shares 才生效)。
    sync_hk_instruments 必须显式要 total_shares/float_shares 这两个 extra_cols。
    """
    provider = FakeProvider(instruments=pl.DataFrame({
        "symbol": ["00700.HK"],
        "name": ["腾讯控股"],
        "code": ["00700"],
        "total_shares": [9_000_000_000.0],
        "float_shares": [9_000_000_000.0],
    }))
    monkeypatch.setattr("app.services.index_sync._get_data_provider", lambda: provider)

    saved: dict[str, pl.DataFrame] = {}

    class SavingRepo:
        def save_hk_instruments(self, df):
            saved["result"] = df

        def refresh_index_views(self):
            pass

    count = index_sync.sync_hk_instruments(SavingRepo())

    assert count == 1
    assert "float_shares" in saved["result"].columns
    assert saved["result"]["float_shares"].to_list() == [9_000_000_000.0]


def test_hk_daily_sync_computes_turnover_rate_when_instruments_present(monkeypatch):
    """端到端(不打桩 compute_enriched):instruments 带 float_shares 时,
    港股 enriched 必须真的算出 turnover_rate,而不是悄悄跳过。
    """
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)

    hk_instruments = pl.DataFrame({
        "symbol": ["00700.HK"],
        "float_shares": [10_000.0],  # volume=1000(FakeProvider 固定值) / 10000 * 100 = 10%
    })
    repo = FakeRepo(hk_instruments=hk_instruments)

    index_sync.sync_and_persist_hk_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["00700.HK"],
    )

    enriched = repo.hk_enriched[0]
    assert "turnover_rate" in enriched.columns
    assert enriched["turnover_rate"].to_list() == [10.0]
    # 港股无涨跌停制度,不应产出涨跌停派生列
    assert "signal_limit_up" not in enriched.columns
    assert "consecutive_limit_ups" not in enriched.columns
