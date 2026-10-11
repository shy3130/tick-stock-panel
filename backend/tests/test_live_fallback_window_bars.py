"""ETF / 指数盘中 enriched (全量回退路径) 的历史窗口必须覆盖最长指标。

ETF 与指数没有 live_agg, 每轮实时行情都走 _flush_live_enriched 的全量回退:
读近 320 个自然日的日K + 今日行情重算指标, 再按交易日裁剪到最长滚动窗口。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

import app.services.quote_service as qs_module
from app.indicators.pipeline import compute_enriched
from app.services.quote_service import QuoteService

TODAY = date(2026, 2, 26)
# 320 日历天窗口跨 2026 元旦与春节休市 (2/16~2/23)
CLOSED = {date(2026, 1, 1), date(2026, 1, 2)} | {date(2026, 2, 16) + timedelta(days=i) for i in range(8)}
SYMBOLS = ("510300.SH", "159915.SZ")
WINDOW_COLUMNS = ("ma60", "ma120", "ma200", "high_60d", "low_60d", "momentum_60d")
TABLES = {"etf": "kline_etf_daily", "index": "kline_index_daily"}


def _trading_days() -> list[date]:
    days: list[date] = []
    d = TODAY - timedelta(days=320)
    while d <= TODAY:
        if d.weekday() < 5 and d not in CLOSED:
            days.append(d)
        d += timedelta(days=1)
    return days


def _bars() -> pl.DataFrame:
    rows = []
    for n, symbol in enumerate(SYMBOLS):
        for i, day in enumerate(_trading_days()):
            close = round(4.0 + n + 0.3 * math.sin(i * 0.23 + n) + 0.002 * i, 3)
            rows.append((symbol, day, close, close + 0.02, close - 0.02, close, 1e6 + 1e4 * i, 4e6))
    return pl.DataFrame(
        rows,
        schema=["symbol", "date", "open", "high", "low", "close", "volume", "amount"],
        orient="row",
    ).sort(["symbol", "date"])


class _StubRepo:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)
        self.flushed: dict[str, pl.DataFrame] = {}

    def get_enriched_latest_asset(self, asset_type: str):
        return pl.DataFrame(), None

    def flush_live_enriched_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        self.flushed[asset_type] = df

    def merge_live_enriched_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        self.flushed[asset_type] = df


def test_fixture_has_enough_trading_days():
    assert len(_trading_days()) >= 200


@pytest.mark.parametrize("asset_type", ["etf", "index"])
def test_fallback_window_counts_trading_days(tmp_path, monkeypatch, asset_type):
    bars = _bars()
    table = tmp_path / TABLES[asset_type]
    for part in bars.filter(pl.col("date") < TODAY).partition_by("date"):
        out = table / f"date={part['date'][0].isoformat()}" / "part.parquet"
        out.parent.mkdir(parents=True)
        part.write_parquet(out)

    monkeypatch.setattr(qs_module, "cn_today", lambda: TODAY)
    qs = QuoteService()
    repo = _StubRepo(tmp_path)
    qs._repo = repo
    qs._flush_live_enriched(
        bars.filter(pl.col("date") == TODAY),
        None,
        asset_type=asset_type,
        merge=asset_type == "index",
    )

    live = repo.flushed[asset_type].sort("symbol")
    full = compute_enriched(bars).filter(pl.col("date") == TODAY).sort("symbol")
    assert live["symbol"].to_list() == full["symbol"].to_list()
    for column in WINDOW_COLUMNS:
        assert full[column].null_count() == 0
        assert live[column].to_list() == pytest.approx(full[column].to_list(), rel=1e-12), column
