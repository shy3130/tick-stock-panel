"""Measure cold matrix axis collection using disposable synthetic partitions.

Run from backend: uv run python -m scripts.benchmark_matrix_axes --symbols 1200 --days 120
"""
from __future__ import annotations

import argparse
import logging
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.dataset as pads

from app.backtest.matrix import _collect_parquet_axes, _matrix_filter_expression


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=1200)
    parser.add_argument("--days", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.symbols <= 5000 or not 1 <= args.days <= 300:
        parser.error("symbols must be 1..5000 and days must be 1..300")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with tempfile.TemporaryDirectory(prefix="matrix-axes-") as directory:
        root = Path(directory)
        first = date(2025, 1, 2)
        symbols = [f"{600000 + i:06d}.SH" for i in range(args.symbols)]
        for offset in range(args.days):
            current = first + timedelta(days=offset)
            part = root / f"date={current}" / "part.parquet"
            part.parent.mkdir(parents=True)
            pl.DataFrame({
                "symbol": symbols,
                "date": [current] * args.symbols,
                "open": [10.0] * args.symbols,
                "high": [10.0] * args.symbols,
                "low": [10.0] * args.symbols,
                "close": [10.0] * args.symbols,
                "volume": [1000.0] * args.symbols,
            }).write_parquet(part)
        last = first + timedelta(days=args.days - 1)
        dataset = pads.dataset(
            str(root), format="parquet",
            partitioning=pads.partitioning(pa.schema([("date", pa.date32())]), flavor="hive"),
        )
        started = time.perf_counter()
        dates, actual_symbols = _collect_parquet_axes(
            dataset, _matrix_filter_expression(first, last, None), batch_size=131_072,
        )
        elapsed = time.perf_counter() - started

        if len(dates) != args.days or actual_symbols != symbols:
            raise AssertionError("matrix axes differ from generated input")

        print(f"rows={args.days * args.symbols} days={len(dates)} "
              f"symbols={len(actual_symbols)} axes_s={elapsed:.3f}")


if __name__ == "__main__":
    main()
