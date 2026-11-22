"""Financial validation shared with the v1 repository integrity checker."""

from datetime import date
from datetime import time as dt_time

import polars as pl

# 尾盘定版线: 实时侧写入的分区一律不算定版 — 15:00 后的 close_final 定版也不
# 可信 (实测有源"时间戳翻新但价格停在竞价前旧价"), 且 15:30 前科创/创业板盘后
# 固定价交易仍在成交 (量未定版)。数据完整以盘后管道 (默认 15:35, 见
# daily_pipeline) 的官方 batch 行覆盖为准; 定版线取盘后固定价终止线 15:30,
# 与 quote_service._FINAL_DEADLINE 的 close_final 重试截止对齐 (15:30 后不再
# 有新实时写入, 判 15:30 与判 15:35 等价)。
CLOSE_CUTOFF = dt_time(15, 30)

DAILY_REQUIRED = ("symbol", "date", "open", "high", "low", "close", "volume")


def _daily_validation(
    frame: pl.DataFrame, day: date | None
) -> tuple[pl.DataFrame, dict[str, pl.Expr]]:
    """数值判据同时用于拦截与错误说明, 停牌零价沿用现有规则。"""
    if not set(DAILY_REQUIRED).issubset(frame.columns):
        raise ValueError("日K字段不完整")
    frame = frame.with_columns(
        pl.col("symbol").cast(pl.String, strict=False),
        pl.col("date").cast(pl.Date, strict=False),
        *(pl.col(c).cast(pl.Float64, strict=False) for c in DAILY_REQUIRED[2:]),
    )
    prices = ("open", "high", "low", "close")
    halted = (
        (pl.col("volume") == 0)
        & (pl.col("open") == 0)
        & (pl.col("high") == 0)
        & (pl.col("low") == 0)
        & (pl.col("close") >= 0)
    )
    checks = {
        "股票代码缺失": pl.col("symbol").is_null() | (pl.col("symbol").str.strip_chars() == ""),
        "日期缺失或非法": pl.col("date").is_null(),
        "成交量缺失、非有限或为负": (
            pl.col("volume").is_null() | ~pl.col("volume").is_finite() | (pl.col("volume") < 0)
        ),
        **{
            f"{label}缺失或非有限": pl.col(column).is_null() | ~pl.col(column).is_finite()
            for column, label in zip(prices, ("开盘价", "最高价", "最低价", "收盘价"), strict=True)
        },
        "非停牌日开高低收存在零价或负价": (
            ~halted.fill_null(False) & pl.any_horizontal(pl.col(c) <= 0 for c in prices)
        ),
        "最高价小于开盘价、收盘价或最低价": (
            ~halted.fill_null(False) & (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        ),
        "最低价大于开盘价、收盘价或最高价": (
            ~halted.fill_null(False) & (pl.col("low") > pl.min_horizontal("open", "close", "high"))
        ),
    }
    if day is not None:
        checks["日期与文件分区不符"] = pl.col("date") != day
    if "amount" in frame.columns:
        amount = pl.col("amount").cast(pl.Float64, strict=False)
        checks["成交额非有限或为负"] = ((amount < 0) | ~amount.is_finite()).fill_null(False)
    return frame, checks


def invalid_daily_rows(frame: pl.DataFrame, day: date | None = None) -> pl.DataFrame:
    """自检与入库校验共享同一判据; 停牌零价与异常数值明确区分。"""
    frame, checks = _daily_validation(frame, day)
    invalid = pl.lit(False)
    for check in checks.values():
        invalid = invalid | check
    return frame.filter(invalid.fill_null(True))
