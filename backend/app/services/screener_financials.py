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
    return (
        pl.col("notice_date").cast(pl.String).str.strptime(pl.Date, format="%Y-%m-%d", strict=False)
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
                pl.col("weight_avg_roe").cast(pl.Float64, strict=False).alias("weight_avg_roe"),
                pl.col("basic_eps").cast(pl.Float64, strict=False).alias("basic_eps"),
                pl.col("gross_margin").cast(pl.Float64, strict=False).alias("gross_margin"),
                pl.col("bps").cast(pl.Float64, strict=False).alias("bps"),
            )
        )
        # 每只股票取最新报告期（同期多次披露时取最新公告）。
        latest = eligible.sort(
            ["symbol", "report_year", "quarter_num", "_notice_date"],
            descending=[False, True, True, True],
        ).unique(subset=["symbol"], keep="first", maintain_order=True)
        # eps_ttm 为近十二个月滚动 EPS（累计财报口径）:
        #   最新报告期为 Q4 → TTM = 本期全年累计EPS；
        #   最新报告期为 Q1/Q2/Q3 → TTM = 本期累计EPS + 上年Q4全年累计EPS − 上年同期累计EPS。
        # 三个输入任一缺失（历史不足、未披露或公告晚于 as_of）或为空 → NULL，绝不外推。
        prior_periods = eligible.sort(
            ["symbol", "report_year", "quarter_num", "_notice_date"],
            descending=[False, True, True, True],
        ).unique(subset=["symbol", "report_year", "quarter_num"], keep="first", maintain_order=True)
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
                        pl.col("basic_eps") + pl.col("_prior_full_eps") - pl.col("_prior_same_eps")
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


_INDUSTRY_HISTORY_SCHEMA = {
    "symbol": pl.String,
    "industry": pl.String,
    "notice_date": pl.Date,
}

# 与行情侧一致的 canonical A 股代码形状 (见 api/market_data.py 的 _SYMBOL_RE)。
_CANONICAL_SYMBOL_RE = r"^\d{6}\.(SH|SZ|BJ)$"


def load_industry_announcements(data_dir: Path) -> pl.DataFrame:
    """Load all (symbol, industry, notice_date) announcement events once.

    市场抱团/拥挤度研究的 PIT 行业历史入口: 返回去重后的全部行业公告事件,
    调用方按 notice_date <= 观察日 自行做 point-in-time 归并。
    与 load_financial_snapshot 语义互不影响(该函数仍按 as_of 取最新报告期)。
    """
    source = data_dir / "financials" / "metrics" / "part.parquet"
    # 先验证 Parquet schema 再投影: 文件不可读(缺失/损坏)仍是 source_unavailable,
    # 缺列映射为 schema_invalid, 而不是让 columns= 投影读取以读失败的形式误报。
    required_columns = ["symbol", "industry", "notice_date", "quarter", "report_year"]
    try:
        schema = pl.read_parquet_schema(source)
    except Exception as exc:
        raise FinancialSnapshotError("source_unavailable") from exc
    if not set(required_columns).issubset(schema.keys()):
        raise FinancialSnapshotError("schema_invalid")
    try:
        frame = pl.read_parquet(source, columns=required_columns)
    except Exception as exc:
        raise FinancialSnapshotError("source_unavailable") from exc

    try:
        quarter = pl.col("quarter").cast(pl.String)
        normalized = (
            frame.with_columns(
                # 沿用项目 canonical 行情约定 (strip + upper + 形状校验):
                # 无法规范化的记录在过滤/分组/冲突检测/输出前整体剔除,
                # 不得静默污染行业映射。
                pl.col("symbol")
                .cast(pl.String)
                .str.strip_chars()
                .str.to_uppercase()
                .alias("symbol"),
                pl.col("industry").cast(pl.String).str.strip_chars().alias("industry"),
                _notice_date_expr(frame.schema["notice_date"]).alias("notice_date"),
                quarter.str.extract(r"^(\d{4})Q([1-4])$", 1)
                .cast(pl.Int64, strict=False)
                .alias("_quarter_year"),
                quarter.str.extract(r"^(\d{4})Q([1-4])$", 2)
                .cast(pl.Int64, strict=False)
                .alias("_quarter_num"),
                pl.col("report_year").cast(pl.Int64, strict=False).alias("report_year"),
            )
            # 沿用 load_financial_snapshot 的年度/quarter 一致性规则, 并在
            # 同日最高季度消解前过滤: 脏季度行不得抬高 PIT 事件的报告期,
            # 也不得参与冲突判定。
            .filter(
                pl.col("symbol").is_not_null()
                & pl.col("symbol").str.contains(_CANONICAL_SYMBOL_RE)
                & pl.col("industry").is_not_null()
                & (pl.col("industry") != "")
                & pl.col("notice_date").is_not_null()
                & pl.col("_quarter_year").is_not_null()
                & pl.col("_quarter_num").is_not_null()
                & pl.col("report_year").is_not_null()
                & (pl.col("_quarter_year") == pl.col("report_year"))
            )
            .with_columns(
                (pl.col("_quarter_year") * 10 + pl.col("_quarter_num")).alias("_quarter_key")
            )
        )
        latest = normalized.filter(
            pl.col("_quarter_key") == pl.col("_quarter_key").max().over(["symbol", "notice_date"])
        )
        conflicts = (
            latest.group_by(["symbol", "notice_date"])
            .agg(pl.col("industry").n_unique().alias("_industry_count"))
            .filter(pl.col("_industry_count") > 1)
        )
        if not conflicts.is_empty():
            raise FinancialSnapshotError("industry_conflict")
        events = (
            latest.unique(subset=["symbol", "notice_date", "industry"])
            .sort(["symbol", "notice_date"])
            .select(["symbol", "industry", "notice_date"])
        )
    except FinancialSnapshotError:
        raise
    except Exception as exc:
        raise FinancialSnapshotError("schema_invalid") from exc

    if events.is_empty():
        return pl.DataFrame(schema=_INDUSTRY_HISTORY_SCHEMA)
    return events
