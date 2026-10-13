"""_resolve_universe 维表单一来源 + 指数过滤测试。"""
import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


class _Capset:
    def __init__(self, has_batch: bool = False):
        self._has_batch = has_batch

    def has(self, cap):
        return self._has_batch


def _write_instruments(tmp_path, symbols):
    out = tmp_path / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols}).write_parquet(out)


def test_resolve_universe_excludes_index_symbols(repo, monkeypatch, tmp_path):
    """自选里的指数不进入股票日K/分钟K同步池。"""
    _write_instruments(tmp_path, ["600000.SH"])
    monkeypatch.setattr(
        daily_pipeline, "get_pool",
        lambda name, refresh=False: ["600000.SH", "000001.SH"] if name == "watchlist" else [],
    )
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: {"000001.SH"})

    universe = daily_pipeline._resolve_universe(_Capset(), repo)
    assert "600000.SH" in universe
    assert "000001.SH" not in universe


def test_resolve_universe_never_fetches_pools(repo, monkeypatch, tmp_path):
    """维表单一来源: 即使有 batch 能力也不拉 CN_Equity_A 池 (SW1 兜底随之消失)。"""
    def _guard(name, refresh=False):
        if name != "watchlist":
            raise AssertionError(f"不得拉取标的池: {name}")
        return ["600000.SH"]

    _write_instruments(tmp_path, ["000001.SZ"])
    monkeypatch.setattr(daily_pipeline, "get_pool", _guard)
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: set())

    universe = daily_pipeline._resolve_universe(_Capset(has_batch=True), repo)
    assert universe == ["000001.SZ", "600000.SH"]


def test_resolve_universe_watchlist_only_when_no_table(repo, monkeypatch, tmp_path):
    """维表缺失时不伪造标的: 池=自选, 宁缺毋假。"""
    monkeypatch.setattr(
        daily_pipeline, "get_pool",
        lambda name, refresh=False: ["600000.SH"] if name == "watchlist" else [],
    )
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: set())

    universe = daily_pipeline._resolve_universe(_Capset(), repo)
    assert universe == ["600000.SH"]
