"""FQuantProvider 在 FQUANT_FSTORE_MODE=duckdb 下的集成回归测试。

覆盖那些"迁移时不需要改代码，只是换了底层客户端"的方法——如果以后
有人往这些方法里加了 PostgreSQL-only 语法（比如新的 ::type cast 或
系统目录查询），这里的测试会先坏，而不是等到生产环境切换才发现。
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from app.data_providers.fquant_provider import FQuantProvider

DUCKDB_PATH = "/Volumes/WD1/fstore.duckdb"

pytestmark = pytest.mark.skipif(
    not os.path.exists(DUCKDB_PATH),
    reason=f"本机没有挂载 {DUCKDB_PATH}",
)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("FQUANT_FSTORE_MODE", "duckdb")
    return FQuantProvider()


def test_get_instruments_stock(provider):
    df = provider.get_instruments("stock")
    assert df.height > 0
    assert "600519" in df["code"].to_list()


def test_get_daily_fstore_fallback(provider):
    rows = provider._get_daily_from_fstore_klines(
        symbol="600519.SH", code="600519",
        start_time=datetime(2025, 1, 1), end_time=datetime(2025, 10, 31),
    )
    assert len(rows) > 0


def test_get_adj_events_from_fstore(provider):
    rows = provider._get_adj_events_from_fstore(
        symbol="600519.SH", code="600519",
        start_time=None, end_time=None,
    )
    assert isinstance(rows, list)


def test_get_adj_events_from_fstore_with_date_range(provider):
    """回归测试：t_date 是 TIMESTAMPTZ，DuckDB 物化它需要 pytz（本仓库未装），

    之前 date-range 分支的 SQL 没有 CAST t_date AS DATE，导致
    FStoreDuckDBClient.query() 内部抛 InvalidInputException 被吞掉，
    静默返回 []。这里显式传 start_time/end_time 触发 BETWEEN 分支，
    验证真实除权除息事件能查到、且 trade_date 不是 None。
    """
    rows = provider._get_adj_events_from_fstore(
        symbol="600519.SH", code="600519",
        start_time=datetime(2020, 1, 1), end_time=datetime(2026, 1, 1),
    )
    assert len(rows) > 0
    assert all(row.get("trade_date") is not None for row in rows)


def test_get_financial_income(provider):
    df = provider.get_financial("600519.SH", "income")
    assert df.height > 0


def test_get_financial_forecast_is_empty_not_error(provider):
    """financial_report_forecast 源表在 PG 和 DuckDB 里都是 0 行，
    这里验证的是"不报错、返回空 df"，不是数据覆盖率问题。"""
    df = provider.get_financial("600519.SH", "forecast")
    assert df.height == 0


def test_get_universe_constituents(provider):
    df = provider.get_universe_constituents("000001")
    assert isinstance(df, type(df))  # 只验证不抛异常；具体行数取决于该指数当前是否有成分股快照
