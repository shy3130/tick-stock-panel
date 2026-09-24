"""多日期分区写入回归: 同一发布对象被复用时, 无变化分区的空提交不能打断整批写入。

`234d31a` 起 ETF enriched 按完整本地历史重算前复权, 一次 `append_etf_enriched`
会写 245+ 个 `date=` 分区; 而 `_write_daily_partition` 对整张表只创建一个
`EnrichedPublication`, 在 `partition_by("date")` 循环里复用。`_write_partition_locked`
对"内容无变化"的分区直接调 `publication.commit()`, 而 commit 提交后 `_changed`
未复位、标记已回到 ready (ready payload 不含 `publication_id`)、`_publishing=False`,
于是第二次 commit 必然命中 `publication_id` 不匹配 → 抛
`enriched publication ownership was lost`。

结果: 只要"有变化的分区"后面跟着一个"无变化的分区", 整批写入就被打断。
盘后管道表现为 `index/etf sync: enriched publication ownership was lost` 恒定失败,
且失败位置固定 (第一批日K响应后 ~0.3s), 与并发无关 —— `recover=True`、清理标记、
停掉实时轮询都不能绕过。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.enriched_generation import EnrichedPublication
from app.tickflow.repository import DataStore, KlineRepository


def _frame(day: str, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [date.fromisoformat(day)],
        "open": [close],
        "high": [close],
        "low": [close],
        "close": [close],
        "volume": [1_000.0],
    })


def _two_days(close_13: float, close_14: float) -> pl.DataFrame:
    return pl.concat([_frame("2026-08-13", close_13), _frame("2026-08-14", close_14)])


def test_repeated_commit_is_idempotent(tmp_path) -> None:
    """分区1 写入并 commit 后, 分区2 无变化 → 空提交必须幂等返回。"""
    publication = EnrichedPublication(tmp_path, recover=True)
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-13" / "part.parquet"
    publication.write_parquet(_frame("2026-08-13", 10.0), out)

    assert publication.commit() is not None
    assert publication.commit() is None


def test_multi_partition_write_tolerates_unchanged_partition(tmp_path) -> None:
    """一次写两个分区, 第二个完全无变化 —— 修复前整批写入会被打断。"""
    repo = KlineRepository(DataStore(tmp_path))

    repo.append_enriched(_two_days(10.0, 11.0))
    # 第一个分区价格变化, 第二个与磁盘完全一致
    repo.append_enriched(_two_days(12.0, 11.0))

    part_13 = tmp_path / "kline_daily_enriched" / "date=2026-08-13" / "part.parquet"
    part_14 = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    assert pl.read_parquet(part_13)["close"].item() == pytest.approx(12.0)
    assert pl.read_parquet(part_14)["close"].item() == pytest.approx(11.0)
