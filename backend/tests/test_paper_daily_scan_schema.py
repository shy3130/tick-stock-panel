"""模拟盘读日线须容忍分区间列差异。

实时行情写入的当日 kline_daily 分区带 quote_ts 列, 管道/历史扩展写入的分区
没有。裸 pl.scan_parquet 以首个文件定 schema, 遇到多列即抛 SchemaError, 被
吞成 None: 昨收取不到(涨跌停校验失效)、次日开盘/收盘单与盘后定版拿不到价格。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.strategy import paper

SYM = "002893.SZ"


def _write(tmp_path, day: date, close: float, *, with_quote_ts: bool) -> None:
    data = {
        "symbol": [SYM], "date": [day],
        "open": [close - 0.1], "high": [close + 0.1], "low": [close - 0.2], "close": [close],
        "volume": [10000.0], "amount": [close * 10000.0],
    }
    if with_quote_ts:
        data["quote_ts"] = [1_790_000_000_000]
    out = tmp_path / "kline_daily" / f"date={day.isoformat()}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(data).write_parquet(out)


def _mixed_partitions(tmp_path) -> None:
    _write(tmp_path, date(2026, 9, 29), 10.29, with_quote_ts=False)
    _write(tmp_path, date(2026, 9, 30), 10.71, with_quote_ts=True)
    _write(tmp_path, date(2026, 10, 8), 10.62, with_quote_ts=False)


def test_prev_close_reads_across_partitions_with_extra_quote_ts(tmp_path):
    _mixed_partitions(tmp_path)

    assert paper._prev_close(tmp_path, SYM, "stock", "2026-10-08") == 10.71


def test_read_daily_bar_reads_across_partitions_with_extra_quote_ts(tmp_path):
    _mixed_partitions(tmp_path)

    assert paper.read_daily_bar(tmp_path, SYM, "stock", "2026-10-08") == pytest.approx({"open": 10.52, "close": 10.62})
    assert paper.read_daily_bar(tmp_path, SYM, "stock", "2026-09-30") == pytest.approx({"open": 10.61, "close": 10.71})


def test_index_close_reads_across_partitions_with_extra_quote_ts(tmp_path):
    for day, close, with_ts in ((date(2026, 9, 30), 4500.0, False), (date(2026, 10, 8), 4520.0, True)):
        data = {
            "symbol": ["000300.SH"], "date": [day], "open": [close], "high": [close],
            "low": [close], "close": [close], "volume": [1.0], "amount": [1.0],
        }
        if with_ts:
            data["quote_ts"] = [1_790_000_000_000]
        out = tmp_path / "kline_index_daily" / f"date={day.isoformat()}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(data).write_parquet(out)

    assert paper._index_close(tmp_path, "2026-10-08") == 4520.0
