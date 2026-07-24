#!/usr/bin/env python
"""一次性清理脚本：移除已入库的停牌脏数据。

背景：历史停牌过滤条件为 "OHLC 全零"，会漏过 close 被数据源填充为
前收盘价的停牌记录（如 *ST 撤销风险警示的停牌日），导致日 K 图出现
开盘价为 0 的异常蜡烛。停牌过滤已改用 "open==0 且 high==0"，本脚本
负责清理既有脏数据，可重复执行（幂等）。

用法（从 backend/ 目录运行）：
    .venv/bin/python -m scripts.cleanup_halt_days          # 清理 + dry-run 关闭
    .venv/bin/python -m scripts.cleanup_halt_days --dry-run # 仅扫描，不写盘
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# kline_daily 原始表与 enriched 表的脏数据都在这些分区里
HALT_TABLES = ["kline_daily", "kline_daily_enriched"]
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
HALT_PRED = (pl.col("open") == 0) & (pl.col("high") == 0)


def _scan_dirty(table: str) -> pl.DataFrame:
    glob = str(DATA_DIR / table / "**" / "*.parquet")
    cast = pl.ScanCastOptions(integer_cast="allow-float")
    return (
        pl.scan_parquet(glob, hive_partitioning=True, cast_options=cast)
        .filter(HALT_PRED)
        .select("symbol", "date")
        .collect()
    )


def _clean_table(table: str, dry_run: bool) -> int:
    """清理单张表所有脏分区，返回被删除的行数。"""
    dirty = _scan_dirty(table)
    if dirty.is_empty():
        logger.info("[%s] 无脏数据", table)
        return 0

    removed = 0
    base = DATA_DIR / table
    for dt in dirty["date"].unique().sort():
        part = base / f"date={dt}" / "part.parquet"
        if not part.exists():
            logger.warning("[%s] 分区文件不存在: %s", table, part)
            continue
        df = pl.read_parquet(part)
        before = df.height
        cleaned = df.filter(~HALT_PRED)
        after = cleaned.height
        diff = before - after
        if diff == 0:
            continue
        removed += diff
        if dry_run:
            logger.info("[%s %s] 将删除 %d 行 (停牌), 剩余 %d 行 [dry-run]",
                        table, dt, diff, after)
            continue
        if after == 0:
            part.unlink()
            logger.info("[%s %s] 删除 %d 行后分区为空, 已移除文件", table, dt, diff)
        else:
            cleaned.write_parquet(part)
            logger.info("[%s %s] 删除 %d 行 (停牌), 剩余 %d 行已重写",
                        table, dt, diff, after)
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(description="清理已入库的停牌脏数据")
    ap.add_argument("--dry-run", action="store_true", help="仅扫描不写盘")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    total = 0
    for table in HALT_TABLES:
        total += _clean_table(table, args.dry_run)

    mode = "dry-run 扫描" if args.dry_run else "已清理"
    logger.info("完成: 共 %s %d 行停牌脏数据", mode, total)


if __name__ == "__main__":
    main()
