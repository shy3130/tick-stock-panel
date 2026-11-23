"""回归: sync_and_persist_daily_batch 必须接受 failed_out 并透出失败标的。

daily_pipeline (#514) 的修复/定向分支传 failed_out=repair_failed_symbols,
但本函数签名漏接该参数 → TypeError, 数据修正/boot 自检修复路径必炸
(v0.3.21/v0.3.22 用户同步失败截图: "got an unexpected keyword argument
'failed_out'")。
"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest

from app.services import kline_sync
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_accepts_failed_out_and_surfaces_failed_symbols(monkeypatch, repo):
    def fake_sync_daily_batch(symbols, **kwargs):
        # 模拟分块拉取失败: 内部经由 failed_out 出参上报失败标的
        kwargs["failed_out"].extend(["600519.SH", "000001.SZ"])
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_daily_batch", fake_sync_daily_batch)
    capset = SimpleNamespace(has=lambda cap: True, limits=lambda cap: None)
    failed: list[str] = []

    written = kline_sync.sync_and_persist_daily_batch(
        ["600519.SH", "000001.SZ"], repo, capset, failed_out=failed)

    assert written == 0
    assert failed == ["600519.SH", "000001.SZ"]


def test_failed_out_omitted_still_works(monkeypatch, repo):
    monkeypatch.setattr(
        kline_sync, "sync_daily_batch", lambda symbols, **kw: pl.DataFrame())
    capset = SimpleNamespace(has=lambda cap: True, limits=lambda cap: None)

    assert kline_sync.sync_and_persist_daily_batch(
        ["600519.SH"], repo, capset) == 0
