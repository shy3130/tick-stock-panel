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
    def __init__(self, hk_instruments: pl.DataFrame | None = None, etf_instruments: pl.DataFrame | None = None):
        self.index_daily = []
        self.etf_daily = []
        self.etf_enriched = []
        self.hk_daily = []
        self.hk_enriched = []
        self._hk_instruments = hk_instruments if hk_instruments is not None else pl.DataFrame()
        self._etf_instruments = etf_instruments if etf_instruments is not None else pl.DataFrame()

    def get_hk_instruments(self):
        return self._hk_instruments

    def get_etf_instruments(self):
        return self._etf_instruments

    def append_index_daily(self, df):
        self.index_daily.append(df)

    def append_index_enriched(self, df):
        pass

    def append_etf_daily(self, df):
        self.etf_daily.append(df)

    def append_etf_enriched(self, df):
        self.etf_enriched.append(df)

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


def test_etf_instruments_sync_keeps_float_shares(monkeypatch):
    """回归测试:与港股同一个 bug —— _fetch_instruments_by_type 原先无条件裁到
    symbol/name/code 三列,ETF instruments 落盘后丢失 float_shares,导致
    换手率永远算不出来。sync_etf_instruments 必须显式要 extra_cols。
    """
    provider = FakeProvider(instruments=pl.DataFrame({
        "symbol": ["510300.SH"],
        "name": ["沪深300ETF"],
        "code": ["510300"],
        "total_shares": [5_000_000_000.0],
        "float_shares": [5_000_000_000.0],
    }))
    monkeypatch.setattr("app.services.index_sync._get_data_provider", lambda: provider)

    saved: dict[str, pl.DataFrame] = {}

    class SavingRepo:
        def save_etf_instruments(self, df):
            saved["result"] = df

        def refresh_index_views(self):
            pass

    count = index_sync.sync_etf_instruments(SavingRepo())

    assert count == 1
    assert "float_shares" in saved["result"].columns
    assert saved["result"]["float_shares"].to_list() == [5_000_000_000.0]


def test_etf_daily_sync_computes_turnover_rate_when_instruments_present(monkeypatch):
    """端到端(不打桩 compute_enriched):ETF instruments 带 float_shares 时,
    enriched 必须真的算出 turnover_rate,且不产涨跌停信号(asset_type="etf"
    不是 "stock",compute_all 不会走 compute_limit_signals 分支)。
    """
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)
    monkeypatch.setattr("app.services.index_sync._load_etf_factors", lambda repo: pl.DataFrame())

    etf_instruments = pl.DataFrame({
        "symbol": ["510300.ETF"],
        "float_shares": [10_000.0],  # volume=1000(FakeProvider 固定值) / 10000 * 100 = 10%
    })
    repo = FakeRepo(etf_instruments=etf_instruments)

    index_sync.sync_and_persist_etf_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["510300.ETF"],
    )

    enriched = repo.etf_enriched[0]
    assert "turnover_rate" in enriched.columns
    assert enriched["turnover_rate"].to_list() == [10.0]
    assert "signal_limit_up" not in enriched.columns
    assert "consecutive_limit_ups" not in enriched.columns


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


def test_hk_daily_sync_survives_chunk_failure(monkeypatch):
    """回归测试:某个 chunk 算 enriched 时抛异常,之前 chunk 循环体内没有
    try/except,异常会直接向上传播 —— 不仅该 chunk 的 raw/enriched 落盘不一致,
    排在它后面的所有 chunk(所有其余 symbol)当天也完全不会被处理。
    hk/etf/index 三处同构,这里测 hk 作为代表。
    """
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)
    monkeypatch.setattr("app.services.preferences.get_index_daily_batch_size", lambda: 1)

    calls = {"n": 0}

    def flaky_compute_enriched(raw, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return raw

    monkeypatch.setattr("app.services.index_sync.compute_enriched", flaky_compute_enriched)

    repo = FakeRepo()
    rows = index_sync.sync_and_persist_hk_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["00700.HK", "00981.HK", "00005.HK"],
    )

    # 第一个 chunk 失败,不能拖累后面两个 chunk 继续处理
    assert rows == 2
    assert len(repo.hk_daily) == 3  # 三个 chunk 的 raw 都已落盘(包括失败那批)
    assert len(repo.hk_enriched) == 2  # 只有成功的两批写了 enriched


def test_index_daily_sync_survives_chunk_failure(monkeypatch):
    """同上,覆盖 sync_and_persist_index_daily(不含 instruments join 的分支)。"""
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)
    monkeypatch.setattr("app.services.preferences.get_index_daily_batch_size", lambda: 1)

    calls = {"n": 0}

    def flaky_compute_enriched(raw, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("boom")
        return raw

    monkeypatch.setattr("app.services.index_sync.compute_enriched", flaky_compute_enriched)

    repo = FakeRepo()
    rows = index_sync.sync_and_persist_index_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["000001.SH", "000300.SH", "399001.SZ"],
    )

    assert rows == 2
    assert len(repo.index_daily) == 3
    # append_index_enriched 是空实现(FakeRepo 不记录),这里只能验证循环没有
    # 因为第 2 个 chunk 失败而提前中止 —— 第 3 个 chunk 必须仍被处理到。
    assert provider.calls[-1][0] == ("399001.SZ",)


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
