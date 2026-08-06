"""Share capital and historical turnover rate production integration.

Loads financial share capital from DuckDB via provider/repository abstractions (no direct connect).
For full rebuild and repair/incremental enriched paths: backward-asof by date to get point-in-time
float_shares, compute turnover_rate = volume / float_shares (decimal), overwrite enriched.turnover_rate using merge-upsert.
If no capital data, keep existing value/null. No fabrication. Stock raw mirror remains read-only.
Retains pipeline staging rebuild and price limit date rules.

The legacy recompute_historical_turnover_test_only only adds _hist_turnover_rate (for tests).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.data_providers.registry import get_active_provider_name, get_provider
from app.storage.repository import KlineRepository
from app.indicators.pipeline import ENRICHED_STORAGE_COLS, _write_enriched_partitions
from app.storage.atomic_write import atomic_write_parquet

logger = logging.getLogger(__name__)


def load_historical_float_shares(
    repo: KlineRepository, 
    symbols: list[str] | None = None, 
    start_date: date | None = None,
) -> pl.DataFrame:
    """Load point-in-time float_shares using existing provider/repository only.
    Loops single-symbol get_financial (per fquant_provider contract). Handles real financial schema
    (notice_date > t_date/report_date for point-in-time, avoid lookahead); returns empty on no data (no fabrication).
    """
    df = pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "float_shares": pl.Float64})
    try:
        provider_name = get_active_provider_name("financial")
        provider = get_provider(provider_name)
        if symbols is None:
            inst = repo.get_instruments()
            if not inst.is_empty() and "symbol" in inst.columns:
                symbols = inst["symbol"].unique().to_list()
        if hasattr(provider, "get_financial") and symbols:
            frames = []
            for sym in (symbols if isinstance(symbols, list) else [symbols]):
                try:
                    part = provider.get_financial(sym, "balance_sheet")
                    if not part.is_empty() and "symbol" in part.columns:
                        frames.append(part)
                except Exception:
                    continue
            if frames:
                capital_df = pl.concat(frames, how="vertical")
                # Real schema priority: notice_date (announce, no lookahead) > t_date/report_date > date
                date_col = "notice_date" if "notice_date" in capital_df.columns else (
                    "t_date" if "t_date" in capital_df.columns else (
                        "report_date" if "report_date" in capital_df.columns else "date"
                    )
                )
                shares_col = next((c for c in capital_df.columns if any(k in c.lower() for k in ["float", "流通", "ltgb", "float_share"])), "float_shares")
                if shares_col in capital_df.columns and date_col in capital_df.columns:
                    df = (capital_df
                          .with_columns([
                              pl.col(date_col).cast(pl.Date).alias("date"),
                              pl.col(shares_col).cast(pl.Float64).alias("float_shares"),
                          ])
                          .select(["symbol", "date", "float_shares"])
                          .filter(pl.col("float_shares") > 0)
                          .sort(["symbol", "date"])
                          .unique(subset=["symbol", "date"]))
    except Exception as e:  # noqa: BLE001
        logger.debug("provider capital load failed: %s", e)
    return df


def recompute_historical_turnover(
    repo: KlineRepository, 
    symbols: list[str] | None = None,
    target_dates: list[date] | None = None,
) -> int:
    """Production path for full rebuild/repair: point-in-time share capital → decimal turnover_rate overwrite using merge-upsert.
    Filters by symbols for incremental efficiency; preserves non-target symbols in date partitions.
    """
    capital_df = load_historical_float_shares(repo, symbols)
    if capital_df.is_empty():
        logger.info("No historical share capital data available")
        return 0

    enriched_base = repo.store.data_dir / "kline_daily_enriched"
    if not enriched_base.exists():
        return 0

    glob_pattern = str(enriched_base / "date=*" / "*.parquet")
    lf = pl.scan_parquet(glob_pattern)
    if target_dates:
        date_strs = [d.isoformat() for d in target_dates]
        lf = lf.filter(pl.col("date").cast(pl.Utf8).is_in(date_strs))
    if symbols:
        lf = lf.filter(pl.col("symbol").is_in(symbols))
    df = lf.collect()
    if df.is_empty():
        return 0

    capital = capital_df.with_columns(pl.col("date").cast(pl.Date)).sort(["symbol", "date"])
    df = df.with_columns(pl.col("date").cast(pl.Date)).sort(["symbol", "date"])
    joined = df.join_asof(
        capital,
        left_on="date",
        right_on="date",
        by="symbol",
        strategy="backward",
    )

    df = joined.with_columns(
        pl.when(
            pl.col("float_shares").is_not_null() & (pl.col("float_shares") > 0)
        )
        .then(pl.col("volume") / pl.col("float_shares"))
        .otherwise(pl.col("turnover_rate"))
        .alias("turnover_rate")
    ).select([c for c in ENRICHED_STORAGE_COLS if c in joined.columns])

    replace_symbols = symbols or (df["symbol"].unique().to_list() if not df.is_empty() else [])
    written = _write_enriched_partitions(enriched_base, df, replace_symbols)

    logger.info("Recomputed point-in-time turnover_rate (decimal, merge-upsert) for %d rows in enriched", written)
    return written


def recompute_historical_turnover_test_only(df: pl.DataFrame, capital_df: pl.DataFrame) -> pl.DataFrame:
    """Test-only legacy that adds _hist_turnover_rate without disk I/O."""
    if df.is_empty() or capital_df.is_empty():
        return df.with_columns(pl.lit(None).alias("_hist_turnover_rate"))
    capital = capital_df.with_columns(pl.col("date").cast(pl.Date)).sort(["symbol", "date"])
    df = df.with_columns(pl.col("date").cast(pl.Date))
    joined = df.join_asof(
        capital,
        left_on="date",
        right_on="date",
        by="symbol",
        strategy="backward",
    )
    drop_cols = [c for c in ["float_shares", "date_right", "float_shares_right"] if c in joined.columns]
    return joined.with_columns(
        pl.when(pl.col("float_shares") > 0)
        .then(pl.col("volume") / pl.col("float_shares"))
        .otherwise(None)
        .alias("_hist_turnover_rate")
    ).drop(drop_cols)


__all__ = ["load_historical_float_shares", "recompute_historical_turnover", "recompute_historical_turnover_test_only"]
