"""Compare daily sync staging read-back with direct promotion.

Run: uv run --frozen python -m scripts.benchmark_daily_sync_staging
Uses synthetic data only; no external data source is contacted.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import polars as pl

ROWS = 100_000
PARTS = 4


def make_parts(root: Path) -> list[Path]:
    symbols = [f"{i:06d}.SZ" for i in range(ROWS)]
    frame = pl.DataFrame({
        "symbol": symbols,
        "date": ["2026-09-18"] * ROWS,
        "open": [10.0] * ROWS,
        "high": [11.0] * ROWS,
        "low": [9.0] * ROWS,
        "close": [10.5] * ROWS,
        "volume": [100.0] * ROWS,
        "amount": [1050.0] * ROWS,
    }).with_columns(pl.Series("bucket", [i % PARTS for i in range(ROWS)]))
    paths = []
    for index, part in enumerate(frame.partition_by("bucket")):
        part = part.drop("bucket")
        path = root / f"part-{index}.parquet"
        part.write_parquet(path)
        paths.append(path)
    return paths


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parts = make_parts(root)
        parts = parts[:PARTS]
        target = root / "target.parquet"

        started = time.perf_counter()
        pl.scan_parquet(parts).collect(engine="streaming").write_parquet(target)
        read_back = time.perf_counter() - started

        target.unlink()
        started = time.perf_counter()
        for index, part in enumerate(parts):
            shutil.copyfile(part, root / f"promoted-{index}.parquet")
        promoted = time.perf_counter() - started

        print(f"parts={len(parts)}, rows={ROWS}")
        print(f"read-back-and-rewrite: {read_back:.4f}s")
        print(f"direct-promotion-copy: {promoted:.4f}s")
        print(f"speedup: {read_back / promoted:.2f}x")


if __name__ == "__main__":
    main()
