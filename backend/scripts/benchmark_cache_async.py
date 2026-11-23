"""Isolated cache and event-loop benchmark; no network or user data.

Run from backend: uv run --frozen python -m scripts.benchmark_cache_async
To compare revisions, run this same file with PYTHONPATH pointing at each backend.
The 250 ms parser delay is deliberately synthetic: it measures event-loop
responsiveness, not CSV parser throughput. All data is generated in a tempdir.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import statistics
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def cache_benchmark(root: Path) -> dict:
    import polars as pl

    from app.backtest.engine import BacktestEngine
    from app.tickflow.repository import DataStore, KlineRepository

    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(168)]
    days = [d for d in days if d.weekday() < 5]
    symbols = [f"{600000 + i}.SH" for i in range(1000)]
    frames = [pl.DataFrame({
        "symbol": symbols, "date": [day] * len(symbols),
        "open": [10.0] * len(symbols), "high": [11.0] * len(symbols),
        "low": [9.0] * len(symbols), "close": [10.0 + i / 100] * len(symbols),
        "volume": [1000.0] * len(symbols),
    }) for i, day in enumerate(days)]
    history = pl.concat(frames)
    repo = KlineRepository(DataStore(root))
    try:
        repo.append_enriched(history)
        repo._enriched_history_cache = history
        repo._enriched_history_start = days[0]
        repo._enriched_history_generation = repo.get_matrix_data_generation()
        if hasattr(repo, "get_enriched_generation_snapshot"):
            repo._enriched_history_snapshot = (history, repo.get_enriched_generation_snapshot())
        engine = BacktestEngine(repo)
        columns = ["symbol", "date", "close"]
        args = (None, days[0], days[-2], columns)
        expected = history.filter(pl.col("date") <= days[-2]).select(columns).sort(["symbol", "date"])
        assert engine.load_panel(*args).equals(expected)
        reads, writes, hits = [], [], 0
        for i in range(30):
            tick = frames[-1].with_columns(pl.lit(12.0 + i / 100).alias("close"))
            started = time.perf_counter()
            repo.flush_live_enriched(tick)
            writes.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            actual = engine.load_panel(*args)
            reads.append((time.perf_counter() - started) * 1000)
            assert actual.equals(expected)
            cached = repo.get_enriched_range(days[0], days[-2], columns=columns)
            if cached is not None:
                assert cached.equals(expected)
                hits += 1
        return {
            "symbols": len(symbols), "partitions": len(days), "rows": history.height,
            "ticks": 30, "historical_cache_hits": hits,
            "panel_computations": engine._cache.stats()["compute_count"],
            "panel_read_median_ms": round(statistics.median(reads), 3),
            "tick_write_median_ms": round(statistics.median(writes), 3),
            "outputs_equal": True,
        }
    finally:
        repo.db.close()


async def async_benchmark(root: Path) -> dict:
    import polars as pl
    from fastapi import UploadFile

    from app.api import ext_data
    from app.services.ext_data import ExtConfig, ExtField

    config = ExtConfig(id="bench", label="bench", mode="snapshot", fields=[
        ExtField("symbol"), ExtField("value", dtype="float"),
    ])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=root)),
    )))

    def parse(*args, **kwargs):
        time.sleep(0.25)
        return pl.DataFrame({"symbol": ["600000.SH"], "value": [1.5]})

    maxima = []
    for _ in range(5):
        intervals = []
        stop = asyncio.Event()

        async def heartbeat(stop=stop, intervals=intervals):
            while not stop.is_set():
                started = time.perf_counter()
                await asyncio.sleep(0.01)
                intervals.append((time.perf_counter() - started) * 1000)

        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.03)
        try:
            with (
                patch.object(ext_data, "_store", lambda _: SimpleNamespace(get=lambda _: config)),
                patch.object(ext_data.pl, "read_csv", parse),
                patch.object(ext_data, "_refresh_views", lambda _: None),
            ):
                file = UploadFile(io.BytesIO(b"symbol,value\n600000.SH,1.5\n"), filename="bench.csv")
                result = await ext_data.upload_data(request, "bench", file, "2026-10-29")
                assert result == {"status": "ok", "rows": 1, "date": "2026-10-29"}
                await file.close()
            await asyncio.sleep(0.03)
        finally:
            stop.set()
            await task
        maxima.append(max(intervals))
    return {
        "synthetic_parser_ms": 250, "heartbeat_period_ms": 10, "runs": 5,
        "median_max_heartbeat_ms": round(statistics.median(maxima), 3),
        "max_heartbeat_runs_ms": [round(value, 3) for value in maxima],
    }


def main():
    logging.basicConfig(level=logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="tsp-hotpaths-") as temporary:
        root = Path(temporary)
        os.environ["DATA_DIR"] = str(root / "settings")
        os.environ["TICKFLOW_ENV_FILE"] = str(root / "absent.env")
        print(json.dumps({
            "cache": cache_benchmark(root / "cache"),
            "async": asyncio.run(async_benchmark(root / "upload")),
        }, indent=2))


if __name__ == "__main__":
    main()
