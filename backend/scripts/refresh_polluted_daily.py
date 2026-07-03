"""Refresh polluted fquant HTTP daily raw partitions and rebuild enriched.

This is a one-shot migration helper for raw parquet partitions written before
the TDX front-adjustment reconstruction fix. It is not used by fquant_local,
where raw mirror writes are disabled.

Usage:
  DATA_PROVIDER=fquant uv run python scripts/refresh_polluted_daily.py --since 2026-07-01
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.config import settings
from app.indicators.pipeline import run_pipeline
from app.services import kline_sync
from app.tickflow.policy import detect_capabilities
from app.storage.repository import KlineRepository


def _collect_symbols(daily_dir: Path, since: date) -> list[str]:
    symbols: set[str] = set()
    for part_dir in sorted(daily_dir.glob("date=*")):
        try:
            part_date = date.fromisoformat(part_dir.name.split("=", 1)[1])
        except (IndexError, ValueError):
            continue
        if part_date < since:
            continue
        part = part_dir / "part.parquet"
        if not part.exists():
            continue
        df = pl.read_parquet(part, columns=["symbol"])
        if not df.is_empty() and "symbol" in df.columns:
            symbols.update(df["symbol"].cast(pl.Utf8).to_list())
    return sorted(symbols)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="first polluted partition date YYYY-MM-DD")
    args = parser.parse_args()
    since = date.fromisoformat(args.since)

    repo = KlineRepository()
    daily_dir = Path(settings.data_dir) / "kline_daily"
    symbols = _collect_symbols(daily_dir, since)
    if not symbols:
        print(f"no symbols found in {daily_dir} since {since}")
        return

    print(f"refreshing {len(symbols)} symbols since {since}")
    capset = detect_capabilities()
    rows = kline_sync.sync_and_persist_daily_batch(
        symbols,
        repo,
        capset,
        start_date=datetime.combine(since, datetime.min.time()),
        end_date=datetime.now(),
    )
    print(f"daily refreshed rows={rows}; rebuilding enriched...")
    enriched_rows = run_pipeline(symbols=symbols, new_dates_only=False)
    print(f"enriched rebuilt rows={enriched_rows}")


if __name__ == "__main__":
    main()
