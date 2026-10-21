"""指数/ETF 日K同步的自定义数据源路由 (#502)。

修复前 sync_and_persist_index_daily / sync_and_persist_etf_daily 经
kline_sync.sync_daily_batch 硬编码走 TickFlow, 无视日K数据源偏好;
修复后与个股 sync_and_persist_daily_batch 同一路由语义:
声明了 daily 数据集的自定义源接管 (asset_type=index/etf), 空手而归回退 TickFlow。
"""
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import index_sync, kline_sync, preferences


class _Caps:
    """TickFlow 能力集替身: has() 恒返回配置值。"""

    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def has(self, cap) -> bool:
        return self.ok


class _FakeProvider:
    """自定义源替身: iter_daily 记录入参并产出预置帧; frames 为空模拟「仅服务股票」。"""

    def __init__(self, frames: list[pl.DataFrame] | None = None, use_get_daily: bool = False) -> None:
        self.frames = frames or []
        self.use_get_daily = use_get_daily
        self.iter_calls: list[dict] = []

    def iter_daily(self, symbols, start_time=None, end_time=None, asset_type="stock", on_chunk_done=None):
        self.iter_calls.append({
            "symbols": list(symbols), "asset_type": asset_type,
            "start_time": start_time, "end_time": end_time,
        })
        yield from self.frames

    def get_daily(self, symbols, start_time=None, end_time=None, asset_type="stock", on_chunk_done=None):
        self.iter_calls.append({
            "symbols": list(symbols), "asset_type": asset_type,
            "start_time": start_time, "end_time": end_time,
        })
        return pl.concat(self.frames, how="diagonal_relaxed") if self.frames else pl.DataFrame()


class _FakeRepo:
    def __init__(self, tmp_path) -> None:
        self.tmp = tmp_path
        self.index_rows: list[pl.DataFrame] = []
        self.index_enriched: list[pl.DataFrame] = []
        self.etf_rows: list[pl.DataFrame] = []
        self.etf_enriched: list[pl.DataFrame] = []
        self.refreshed = 0

    @property
    def store(self):
        return SimpleNamespace(data_dir=self.tmp)

    def get_index_instruments(self):
        return pl.DataFrame({"symbol": ["000001.SH", "399001.SZ"]})

    def get_etf_instruments(self):
        return pl.DataFrame({"symbol": ["510300.SH"]})

    def append_index_daily(self, df):
        self.index_rows.append(df)

    def append_index_enriched(self, df):
        self.index_enriched.append(df)

    def append_etf_daily(self, df):
        self.etf_rows.append(df)

    def append_etf_enriched(self, df):
        self.etf_enriched.append(df)

    def refresh_index_views(self):
        self.refreshed += 1


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """路由到 fake_src 并隔离 TickFlow 路径副作用; 返回可配置的测试上下文。"""
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fake_src")
    # TickFlow 路径副作用隔离: 限速/批量计算与网络调用全部替身化
    tickflow_calls: list[list[str]] = []

    def _fake_sync_daily_batch(symbols, **kwargs):
        tickflow_calls.append(list(symbols))
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_daily_batch", _fake_sync_daily_batch)
    monkeypatch.setattr(index_sync, "compute_enriched", lambda df, factors=None, instruments=None: df)
    monkeypatch.setattr(index_sync, "resolve_limit", lambda capset, cap, **kw: SimpleNamespace(batch=100, rpm=600))
    monkeypatch.setattr(index_sync, "min_batch", lambda size, limit: 50)
    monkeypatch.setattr(index_sync, "sleep_between_batches", lambda i, rpm: None)
    return SimpleNamespace(tmp_path=tmp_path, monkeypatch=monkeypatch, tickflow_calls=tickflow_calls)


def _route(harness, provider):
    from app.data_providers import custom as custom_sources

    harness.monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: name == "fake_src" and ds == "daily")
    harness.monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)


def _index_df() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SH", "399001.SZ"],
        "date": ["2026-10-20", "2026-10-20"],
        "close": [3813.79, 13118.62],
    })


def _etf_df() -> pl.DataFrame:
    return pl.DataFrame({"symbol": ["510300.SH"], "date": ["2026-10-20"], "close": [4.236]})


# ---------------------------------------------------------------- 指数日K
def test_custom_provider_takes_over_index_daily(harness):
    provider = _FakeProvider(frames=[_index_df()])
    _route(harness, provider)
    repo = _FakeRepo(harness.tmp_path)

    # TickFlow 无 KLINE_DAILY_BATCH 能力 (纯自定义源部署) 也要能同步成功
    rows = index_sync.sync_and_persist_index_daily(repo, _Caps(ok=False))

    assert rows == 2
    assert provider.iter_calls[0]["asset_type"] == "index"
    assert provider.iter_calls[0]["symbols"] == ["000001.SH", "399001.SZ"]
    assert repo.index_rows and repo.index_rows[0].height == 2
    assert repo.index_enriched and repo.index_enriched[0].height == 2
    assert repo.refreshed == 1
    assert harness.tickflow_calls == []  # 不再走 TickFlow


def test_custom_index_daily_via_get_daily_only_provider(harness):
    """仅实现 get_daily (无 iter_daily) 的源: 包装为单帧, 同样接管。"""
    provider = _FakeProvider(frames=[_index_df()], use_get_daily=True)
    _route(harness, provider)
    provider.iter_daily = None  # 摘掉 iter_daily, 强制走 get_daily 分支
    repo = _FakeRepo(harness.tmp_path)

    rows = index_sync.sync_and_persist_index_daily(repo, _Caps(ok=True))

    assert rows == 2
    assert provider.iter_calls[0]["asset_type"] == "index"
    assert harness.tickflow_calls == []


def test_custom_provider_stock_only_falls_back_to_tickflow(harness):
    """源对 asset_type=index 空手而归 (fuyao/tdxquant/amber 均如此) → 回退 TickFlow。"""
    provider = _FakeProvider(frames=[])
    _route(harness, provider)
    repo = _FakeRepo(harness.tmp_path)

    rows = index_sync.sync_and_persist_index_daily(repo, _Caps(ok=True))

    assert rows == 0
    assert repo.index_rows == []
    assert len(harness.tickflow_calls) >= 1  # 回退到 TickFlow 路径
    assert repo.refreshed == 1


def test_provider_without_daily_dataset_falls_back(harness):
    from app.data_providers import custom as custom_sources

    harness.monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: False)
    repo = _FakeRepo(harness.tmp_path)

    rows = index_sync.sync_and_persist_index_daily(repo, _Caps(ok=True))

    assert rows == 0
    assert len(harness.tickflow_calls) >= 1


def test_tickflow_provider_keeps_original_path(harness):
    harness.monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    repo = _FakeRepo(harness.tmp_path)

    rows = index_sync.sync_and_persist_index_daily(repo, _Caps(ok=True))

    assert rows == 0
    assert len(harness.tickflow_calls) >= 1
    assert repo.index_rows == []


# ---------------------------------------------------------------- ETF 日K
def test_custom_provider_takes_over_etf_daily(harness):
    provider = _FakeProvider(frames=[_etf_df()])
    _route(harness, provider)
    repo = _FakeRepo(harness.tmp_path)

    rows = index_sync.sync_and_persist_etf_daily(repo, _Caps(ok=False))

    assert rows == 1
    assert provider.iter_calls[0]["asset_type"] == "etf"
    assert repo.etf_rows and repo.etf_rows[0].height == 1
    assert repo.etf_enriched and repo.etf_enriched[0].height == 1
    assert harness.tickflow_calls == []


def test_custom_etf_stock_only_falls_back_to_tickflow(harness):
    provider = _FakeProvider(frames=[])
    _route(harness, provider)
    repo = _FakeRepo(harness.tmp_path)

    rows = index_sync.sync_and_persist_etf_daily(repo, _Caps(ok=True))

    assert rows == 0
    assert len(harness.tickflow_calls) >= 1
