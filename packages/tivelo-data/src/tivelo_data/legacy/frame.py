"""Pure legacy frame rules shared by data normalization and enriched features."""

import polars as pl


def filter_halt_days(df: pl.DataFrame) -> pl.DataFrame:
    """保留 v1 停牌过滤口径：开盘/最高为零，或历史填价但成交量/额全零。"""
    if df.is_empty() or "open" not in df.columns or "high" not in df.columns:
        return df
    return df.filter(~halted_day(df))


def halted_day(df: pl.DataFrame) -> pl.Expr:
    if "volume" in df.columns and "amount" in df.columns:
        return halted_daily_values(
            pl.col("open"), pl.col("high"), pl.col("volume"), pl.col("amount")
        )
    return (pl.col("open") == 0) & (pl.col("high") == 0)


def halted_daily_values(open_price, high, volume, amount):
    """The v1 halt predicate accepts either numeric values or Polars expressions."""
    return ((open_price == 0) & (high == 0)) | ((volume == 0) & (amount == 0))
