"""_resolve_universe 指数过滤测试。"""
import pytest

from app.jobs import daily_pipeline
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_resolve_universe_excludes_index_symbols(repo, monkeypatch, tmp_path):
    """自选里的指数不进入股票日K/分钟K同步池。"""
    class _Capset:
        def has(self, cap):
            return False

    monkeypatch.setattr(
        daily_pipeline, "get_pool",
        lambda name, refresh=False: ["600000.SH", "000001.SH"] if name == "watchlist" else [],
    )
    monkeypatch.setattr(daily_pipeline, "DEMO_SYMBOLS", [])
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: {"000001.SH"})

    universe = daily_pipeline._resolve_universe(_Capset(), repo)
    assert "600000.SH" in universe
    assert "000001.SH" not in universe
