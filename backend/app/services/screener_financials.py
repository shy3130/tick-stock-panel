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
    "eps_annualized",
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
    "eps_annualized": pl.Float64,
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
        frame = (
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
            .sort(
                ["symbol", "report_year", "quarter_num", "_notice_date"],
                descending=[False, True, True, True],
            )
            .unique(subset=["symbol"], keep="first", maintain_order=True)
            .with_columns(
                (pl.col("basic_eps") / pl.col("quarter_num") * 4).alias("eps_annualized")
            )
        )
    except Exception as exc:
        raise FinancialSnapshotError("schema_invalid") from exc

    if frame.is_empty():
        return _empty_snapshot()
    return frame.select(_OUTPUT_COLUMNS)
