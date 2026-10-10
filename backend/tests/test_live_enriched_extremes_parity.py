"""盘中增量 enriched 的 60 日极值与盘后全量口径一致。

盘后全量 (compute_indicators)、回测矩阵 (matrix high_60d/low_60d) 与因子
distance_to_high_60d 的「60日最高/最低」都是 60 根收盘价的极值; 盘中增量路径
(live_agg + compute_enriched_today) 若改用最高价/最低价, 盘中 high_60d 会被
历史上影线抬高, 「创60日新高/新低」(close >= high_60d) 几乎不可能成立,
收盘后全量重算或盘中重启又变回收盘价口径, 同一时刻的值与信号前后跳变。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.indicators.pipeline import (
    compute_enriched_today,
    compute_indicators,
    compute_signals,
)
from app.tickflow.repository import DataStore, KlineRepository

TODAY = date(2026, 8, 3)


def _trading_days(count: int) -> list[date]:
    days: list[date] = []
    d = TODAY - timedelta(days=1)
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def _bars() -> pl.DataFrame:
    """两只票 80 个历史交易日 + 今日。

    600001.SH 收盘价缓涨且每天带 +0.5 的长上影; 今日收盘创 60 日收盘新高,
    但仍低于昨日上影高点。000002.SZ 镜像构造: 缓跌 + 长下影, 今日收盘创 60 日
    收盘新低, 但仍高于昨日下影低点。
    """
    rows = []
    days = _trading_days(80)
    for i, d in enumerate(days):
        up = 10.0 + 0.01 * i
        down = 20.0 - 0.01 * i
        rows.append(("600001.SH", d, up, up + 0.5, up - 0.05, up))
        rows.append(("000002.SZ", d, down, down + 0.05, down - 0.5, down))
    last_up = 10.0 + 0.01 * 79
    last_down = 20.0 - 0.01 * 79
    rows.append(("600001.SH", TODAY, last_up, last_up + 0.03, last_up - 0.05, last_up + 0.02))
    rows.append(("000002.SZ", TODAY, last_down, last_down + 0.05, last_down - 0.03, last_down - 0.02))
    return pl.DataFrame(
        rows,
        schema=["symbol", "date", "open", "high", "low", "close"],
        orient="row",
    ).with_columns(
        pl.lit(1000.0).alias("volume"),
        (pl.col("close") * 1000.0).alias("amount"),
        pl.col("close").alias("raw_close"),
        pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"),
    ).sort(["symbol", "date"])


def _incremental_and_full(tmp_path) -> tuple[pl.DataFrame, pl.DataFrame]:
    bars = _bars()
    history = bars.filter(pl.col("date") < TODAY)
    history_indicators = compute_indicators(history)
    latest = history["date"].max()

    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_history_cache = history_indicators
    repo._build_live_agg(latest)

    today_ohlcv = bars.filter(pl.col("date") == TODAY).drop("raw_close", "raw_high", "raw_low")
    incremental = compute_enriched_today(
        repo.get_live_agg(),
        history_indicators.filter(pl.col("date") == latest),
        today_ohlcv,
        None,
    ).sort("symbol")
    full = compute_signals(compute_indicators(bars)).filter(pl.col("date") == TODAY).sort("symbol")
    return incremental, full


@pytest.fixture(autouse=True)
def _migrated_signal_definitions(monkeypatch):
    """注入迁移自内置的信号定义 (信号列定义驱动, 不依赖运行目录)。"""
    from tests.signal_seeds import install_pipeline_caches
    install_pipeline_caches(monkeypatch)


def test_intraday_60d_extremes_match_full_close_basis(tmp_path):
    incremental, full = _incremental_and_full(tmp_path)

    for column in ("high_60d", "low_60d"):
        assert incremental[column].to_list() == full[column].to_list(), column


def test_intraday_n_day_high_low_signals_match_full(tmp_path):
    incremental, full = _incremental_and_full(tmp_path)
    by_symbol = {row["symbol"]: row for row in incremental.iter_rows(named=True)}

    # 盘后全量: 今日收盘分别创 60 日收盘新高/新低
    assert full.filter(pl.col("symbol") == "600001.SH")["signal_n_day_high"].item() is True
    assert full.filter(pl.col("symbol") == "000002.SZ")["signal_n_day_low"].item() is True
    # 盘中增量须同口径命中
    assert by_symbol["600001.SH"]["signal_n_day_high"] is True
    assert by_symbol["000002.SZ"]["signal_n_day_low"] is True
    assert incremental["signal_n_day_high"].to_list() == full["signal_n_day_high"].to_list()
    assert incremental["signal_n_day_low"].to_list() == full["signal_n_day_low"].to_list()
