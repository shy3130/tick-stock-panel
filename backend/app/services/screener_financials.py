"""Point-in-time financial snapshot loading for the condition screener."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import ClassVar

import polars as pl

_REQUIRED_COLUMNS = {
    "symbol",
    "report_year",
    "quarter",
    "notice_date",
    "basic_eps",
    "bps",
    "weight_avg_roe",
    "gross_margin",
    "industry",
    "yo_y_profit",
}
_OUTPUT_COLUMNS = [
    "symbol",
    "industry",
    "yo_y_profit",
    "weight_avg_roe",
    "basic_eps",
    "gross_margin",
    "bps",
    "eps_ttm",
    "report_year",
    "quarter_num",
]
_OUTPUT_SCHEMA = {
    "symbol": pl.String,
    "industry": pl.String,
    "yo_y_profit": pl.Float64,
    "weight_avg_roe": pl.Float64,
    "basic_eps": pl.Float64,
    "gross_margin": pl.Float64,
    "bps": pl.Float64,
    "eps_ttm": pl.Float64,
    "report_year": pl.Int64,
    "quarter_num": pl.Int64,
}


class FinancialSnapshotError(RuntimeError):
    """Sanitized failure signal for unavailable or malformed snapshot data."""

    _MESSAGES: ClassVar[dict[str, str]] = {
        "source_unavailable": "financial snapshot unavailable",
        "schema_invalid": "financial snapshot schema invalid",
    }

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(self._MESSAGES.get(reason, "financial snapshot unavailable"))


def _empty_snapshot() -> pl.DataFrame:
    return pl.DataFrame(schema=_OUTPUT_SCHEMA)


def _notice_date_expr(dtype: pl.DataType) -> pl.Expr:
    if dtype == pl.Date:
        return pl.col("notice_date")
    if dtype == pl.Datetime:
        return pl.col("notice_date").cast(pl.Date)
    return pl.col("notice_date").cast(pl.String).str.strptime(
        pl.Date, format="%Y-%m-%d", strict=False
    )


def load_financial_snapshot(data_dir: Path, as_of: date) -> pl.DataFrame:
    """Load the latest announced financial row per symbol as of ``as_of``."""
    source = data_dir / "financials" / "metrics" / "part.parquet"
    try:
        frame = pl.read_parquet(source)
    except Exception as exc:
        raise FinancialSnapshotError("source_unavailable") from exc

    if not _REQUIRED_COLUMNS.issubset(frame.columns):
        raise FinancialSnapshotError("schema_invalid")

    try:
        quarter = pl.col("quarter").cast(pl.String)
        eligible = (
            frame.with_columns(
                _notice_date_expr(frame.schema["notice_date"]).alias("_notice_date"),
                quarter.str.extract(r"^(\d{4})Q([1-4])$", 1)
                .cast(pl.Int64, strict=False)
                .alias("_quarter_year"),
                quarter.str.extract(r"^(\d{4})Q([1-4])$", 2)
                .cast(pl.Int64, strict=False)
                .alias("quarter_num"),
                pl.col("report_year").cast(pl.Int64, strict=False).alias("report_year"),
            )
            .filter(
                pl.col("symbol").is_not_null()
                & pl.col("_notice_date").is_not_null()
                & (pl.col("_notice_date") <= pl.lit(as_of))
                & pl.col("_quarter_year").is_not_null()
                & pl.col("quarter_num").is_not_null()
                & pl.col("report_year").is_not_null()
                & (pl.col("_quarter_year") == pl.col("report_year"))
            )
            .with_columns(
                pl.col("symbol").cast(pl.String).alias("symbol"),
                pl.col("industry").cast(pl.String).alias("industry"),
                pl.col("yo_y_profit").cast(pl.Float64, strict=False).alias("yo_y_profit"),
                pl.col("weight_avg_roe")
                .cast(pl.Float64, strict=False)
                .alias("weight_avg_roe"),
                pl.col("basic_eps").cast(pl.Float64, strict=False).alias("basic_eps"),
                pl.col("gross_margin").cast(pl.Float64, strict=False).alias("gross_margin"),
                pl.col("bps").cast(pl.Float64, strict=False).alias("bps"),
            )
        )
        # 每只股票取最新报告期（同期多次披露时取最新公告）。
        latest = (
            eligible.sort(
                ["symbol", "report_year", "quarter_num", "_notice_date"],
                descending=[False, True, True, True],
            )
            .unique(subset=["symbol"], keep="first", maintain_order=True)
        )
        # eps_ttm 为近十二个月滚动 EPS（累计财报口径）:
        #   最新报告期为 Q4 → TTM = 本期全年累计EPS；
        #   最新报告期为 Q1/Q2/Q3 → TTM = 本期累计EPS + 上年Q4全年累计EPS − 上年同期累计EPS。
        # 三个输入任一缺失（历史不足、未披露或公告晚于 as_of）或为空 → NULL，绝不外推。
        prior_periods = (
            eligible.sort(
                ["symbol", "report_year", "quarter_num", "_notice_date"],
                descending=[False, True, True, True],
            )
            .unique(subset=["symbol", "report_year", "quarter_num"], keep="first", maintain_order=True)
        )
        # 上年同期累计 EPS (报告期年份对齐: report_year+1 的同期)。
        prior_same = prior_periods.select(
            "symbol",
            (pl.col("report_year") + 1).alias("_ttm_match_year"),
            "quarter_num",
            pl.col("basic_eps").alias("_prior_same_eps"),
        )
        # 上年 Q4 全年累计 EPS (仅年份对齐)。
        prior_full = prior_periods.filter(pl.col("quarter_num") == 4).select(
            "symbol",
            (pl.col("report_year") + 1).alias("_ttm_match_year"),
            pl.col("basic_eps").alias("_prior_full_eps"),
        )
        frame = (
            latest.join(
                prior_same,
                left_on=["symbol", "report_year", "quarter_num"],
                right_on=["symbol", "_ttm_match_year", "quarter_num"],
                how="left",
            )
            .join(
                prior_full,
                left_on=["symbol", "report_year"],
                right_on=["symbol", "_ttm_match_year"],
                how="left",
            )
            .with_columns(
                pl.when(pl.col("quarter_num") == 4)
                .then(pl.col("basic_eps"))
                .otherwise(
                    pl.when(
                        pl.col("basic_eps").is_not_null()
                        & pl.col("_prior_same_eps").is_not_null()
                        & pl.col("_prior_full_eps").is_not_null()
                    )
                    .then(
                        pl.col("basic_eps")
                        + pl.col("_prior_full_eps")
                        - pl.col("_prior_same_eps")
                    )
                    .otherwise(None)
                )
                .alias("eps_ttm")
            )
        )
    except Exception as exc:
        raise FinancialSnapshotError("schema_invalid") from exc

    if frame.is_empty():
        return _empty_snapshot()
    return frame.select(_OUTPUT_COLUMNS)
