"""EngineDataDuckDBClient 完整契约测试。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant.engine_data_duckdb_client import EngineDataDuckDBClient, _prefixed_code

TDX_PATH = "/Volumes/WD1/tdx.duckdb"
TDX_MINUTES_PATH = "/Volumes/WD1/tdx-minutes.duckdb"
TDX_TRANS_PATH = "/Volumes/WD1/tdx-trans.duckdb"


def test_prefixed_code():
    assert _prefixed_code("600519") == "sh600519"
    assert _prefixed_code("000001") == "sz000001"
    assert _prefixed_code("300059") == "sz300059"
    assert _prefixed_code("830799") == "bj830799"


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_day_returns_rows():
    client = EngineDataDuckDBClient()
    rows = client.get_day("600519", limit=5)
    assert len(rows) > 0
    for key in ("date", "open", "close", "high", "low", "volume", "amount"):
        assert key in rows[0]


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_wide_returns_rows():
    client = EngineDataDuckDBClient()
    rows = client.get_wide("600519", limit=5)
    assert len(rows) > 0
    for key in ("open", "last_close", "change_rate", "inner_volume", "outer_volume"):
        assert key in rows[0]


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_xdxr_returns_rows_with_aliased_column():
    client = EngineDataDuckDBClient()
    rows = client.get_xdxr("600519", limit=5)
    assert len(rows) > 0
    assert "xingquanjia" in rows[0]  # 键名是 xingquanjia 不是 xingquanjiya
    # 已知限制：market_xdxr.xingquanjiya 全表都是 NULL（engine 侧写入 bug，
    # 见任务背景），这里断言值为 None 是记录现状，不是期望值——等 engine 那边
    # 修好后这一行断言要改成非 None，否则会一直"假装通过"掩盖数据已经修复的事实。
    assert rows[0]["xingquanjia"] is None


@pytest.mark.skipif(not os.path.exists(TDX_MINUTES_PATH), reason=f"本机没有 {TDX_MINUTES_PATH}")
def test_get_minutes_returns_price_volume_shape():
    client = EngineDataDuckDBClient()
    rows = client.get_minutes("600519", "20260706", limit=5)
    assert len(rows) > 0
    assert set(rows[0].keys()) == {"price", "volume"}


@pytest.mark.skipif(not os.path.exists(TDX_TRANS_PATH), reason=f"本机没有 {TDX_TRANS_PATH}")
def test_get_trans_returns_rows_with_expected_shape():
    client = EngineDataDuckDBClient()
    rows = client.get_trans("600519", "20260706", limit=10)
    assert len(rows) > 0
    for key in ("time", "price", "volume", "amount", "order_count", "direction"):
        assert key in rows[0]
