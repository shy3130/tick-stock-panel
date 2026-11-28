"""历史股本解析。

财务股本按公告日可用，历史缺失时回退 instruments 最新流通股本。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl


def load_share_history(data_dir: Path) -> pl.DataFrame:
    """读取本地财务股本表；未同步或损坏时返回空表。"""
    path = data_dir / "financials" / "shares" / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        shares = pl.read_parquet(path, glob=False)
        if not {"symbol", "period_end", "float_shares"} <= set(shares.columns):
            return pl.DataFrame()
        return shares
    except Exception:
        return pl.DataFrame()


def apply_historical_float_shares(
    rows: pl.DataFrame,
    shares: pl.DataFrame | None,
    *,
    today: date,
) -> pl.DataFrame:
    """为行情行解析有效流通股本。

    当日保留 rows.float_shares；历史日期使用公告日不晚于交易日的最新股本，
    找不到历史记录时继续使用 rows.float_shares。
    """
    required = {"symbol", "date", "float_shares"}
    if (
        rows.is_empty()
        or not required <= set(rows.columns)
        or shares is None
        or shares.is_empty()
        or not {"symbol", "period_end", "float_shares"} <= set(shares.columns)
    ):
        return rows

    def as_date_expr(column: str) -> pl.Expr:
        dtype = shares.schema[column]
        if dtype == pl.Utf8:
            return pl.col(column).str.to_date(strict=False)
        return pl.col(column).cast(pl.Date, strict=False)

    # 严格按公告日生效 (CONTRIBUTING §3.4): 缺失 announce_date 不可回退到 period_end,
    # 否则年报 12-31 会在次年 3-4 月公告前提前生效造成未来函数。
    if "announce_date" not in shares.columns:
        return rows
    available_date = as_date_expr("announce_date")

    history = (
        shares
        .select(
            pl.col("symbol").cast(pl.Utf8),
            available_date.alias("_share_available_date"),
            pl.col("period_end").cast(pl.Utf8).alias("_share_period_end"),
            pl.col("float_shares").cast(pl.Float64, strict=False).alias("_historical_float_shares"),
        )
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("_share_available_date").is_not_null()
            & (pl.col("_historical_float_shares") > 0)
        )
        .sort(["symbol", "_share_available_date", "_share_period_end"])
        .unique(subset=["symbol", "_share_available_date"], keep="last")
        .sort(["symbol", "_share_available_date"])
    )
    if history.is_empty():
        return rows

    resolved = (
        rows
        .with_row_index("_share_row_order")
        .with_columns(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("date").cast(pl.Date, strict=False).alias("_share_trade_date"),
        )
        .sort(["symbol", "_share_trade_date"])
        .join_asof(
            history,
            left_on="_share_trade_date",
            right_on="_share_available_date",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
        .with_columns(
            pl.when(pl.col("_share_trade_date") == pl.lit(today))
            .then(pl.col("float_shares"))
            .otherwise(
                pl.coalesce("_historical_float_shares", "float_shares")
            )
            .alias("float_shares")
        )
        .sort("_share_row_order")
    )
    return resolved.drop(
        "_share_row_order",
        "_share_trade_date",
        "_share_available_date",
        "_share_period_end",
        "_historical_float_shares",
    )
