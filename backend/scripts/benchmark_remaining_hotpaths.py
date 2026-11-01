"""Offline, isolated benchmark for projection, overview and ETF history caching.

Run with the backend on PYTHONPATH; use the same interpreter/script against an
archived baseline to compare. Each run creates and removes its own synthetic
data. No provider calls or real user data are used. Timings exclude fixture
generation and ETF disk output; output digests must match across revisions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import polars as pl


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def timed(fn, runs=7) -> dict:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {"median_ms": round(statistics.median(samples), 3),
            "runs_ms": [round(x, 3) for x in samples]}


def ext_benchmark(root: Path) -> dict:
    from app.api import ext_data
    from app.services.ext_data import ExtConfig, ExtField

    rng = np.random.default_rng(7)
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
    for day in days:
        path = root / "ext_data/bench/timeseries" / f"date={day}" / "part.parquet"
        path.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": [f"{i:06}.SZ" for i in range(2000)], **{
            f"metric_{i}": rng.random(2000) for i in range(30)
        }}).write_parquet(path)
    cfg = ExtConfig(id="bench", label="bench", mode="timeseries", fields=[ExtField("symbol")])
    materialized = []
    original = ext_data._with_instrument_name

    def inspect(frame, data_dir):
        materialized.append(frame.estimated_size())
        return original(frame, data_dir)

    def query():
        return ext_data.list_rows(None, "bench", snapshot_date=None, columns="metric_0",
                                  limit=100, offset=0, filter=None, sort=None,
                                  start_date=str(days[0]), end_date=str(days[-1]))

    with (patch.object(ext_data, "_store", lambda _: SimpleNamespace(get=lambda _: cfg)),
          patch.object(ext_data, "_data_dir", lambda _: root),
          patch.object(ext_data, "_with_instrument_name", inspect)):
        result = query()
        timing = timed(query)
    assert result["total"] == 240000 and len(result["rows"]) == 100
    return {"partitions": 120, "rows": 240000, "stored_columns": 31,
            "materialized_bytes": materialized[-1], "output_digest": digest(result), **timing}


def overview_benchmark() -> dict:
    from app.api import overview

    counts, durations = [], []
    for _ in range(7):
        overview.invalidate_overview_cache()
        barrier = threading.Barrier(8)
        builds = []

        def build(*args, builds=builds):
            builds.append(1)
            # Controlled miss window: only duplicate-build counts are evidence
            # about production work; this sleep is not a latency speedup claim.
            time.sleep(0.04)
            return {"market": {"symbols": 5500, "up": 2100, "down": 3100}}

        def request(_, barrier=barrier):
            barrier.wait(timeout=5)
            return overview.market_overview(None)

        with patch.object(overview, "_build_overview", build), ThreadPoolExecutor(8) as pool:
            start = time.perf_counter()
            result = list(pool.map(request, range(8)))
            durations.append((time.perf_counter() - start) * 1000)
        assert all(row == result[0] for row in result)
        counts.append(len(builds))
    return {"concurrent_requests": 8, "builds_per_burst": counts,
            "synthetic_build_delay_ms": 40, "output_digest": digest(result),
            "median_burst_ms": round(statistics.median(durations), 3)}


def etf_benchmark(root: Path) -> dict:
    from app.indicators import pipeline
    from app.services import quote_service

    today, n = date(2026, 10, 29), 500
    days = sorted(today - timedelta(days=i) for i in range(1, 145)
                  if (today - timedelta(days=i)).weekday() < 5)
    for j, day in enumerate(days):
        frame = pl.DataFrame({
            "symbol": [f"{510000+i}.SH" for i in range(n)], "date": [day] * n,
            "open": [10. + i % 30 * .1 for i in range(n)],
            "high": [12. + i % 30 * .1 for i in range(n)],
            "low": [9. + i % 30 * .1 for i in range(n)],
            "close": [10.5 + i % 30 * .1 + j * .001 for i in range(n)],
            "volume": [100000. + i + j for i in range(n)],
            "amount": [1000000. + i + j for i in range(n)],
        })
        path = root / "kline_etf_daily" / f"date={day}" / "part.parquet"
        path.parent.mkdir(parents=True)
        frame.write_parquet(path)
    current = frame.with_columns(pl.lit(today).alias("date"))
    outputs, scans, computes = [], [], []
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=root),
                           get_enriched_latest_asset=lambda _: (current, today),
                           flush_live_enriched_asset=lambda asset, df: outputs.append(df))
    service = quote_service.QuoteService()
    service.set_repo(repo)
    collect, compute = quote_service.guarded_collect, pipeline.compute_enriched

    def counted_scan(*args, **kwargs):
        scans.append(1)
        return collect(*args, **kwargs)

    def counted_compute(frame, *args, **kwargs):
        computes.append(frame.height)
        return compute(frame, *args, **kwargs)

    with (patch.object(quote_service, "cn_today", lambda: today),
          patch.object(quote_service, "guarded_collect", counted_scan),
          patch.object(pipeline, "compute_enriched", counted_compute)):
        cold = timed(lambda: service._flush_live_enriched(current, asset_type="etf"), runs=1)
        for _ in range(2):
            service._flush_live_enriched(current, asset_type="etf")
        scans.clear()
        warm = timed(lambda: service._flush_live_enriched(current, asset_type="etf"), runs=21)
    assert len(outputs) == 24 and all(df.height == n for df in outputs)
    hashes = [digest(df.sort("symbol").to_dicts()) for df in outputs]
    assert len(set(hashes)) == 1
    cache = getattr(service, "_etf_history_cache", None)
    return {"symbols": n, "history_partitions": len(days), "warm_history_scans": len(scans),
            "warm_updates": 21, "full_compute_rows": computes[-1],
            "cache_bytes": cache[1].estimated_size() if cache else 0,
            "cold_ms": cold["median_ms"], "output_digest": hashes[0], **warm}


def memory_benchmark(root: Path) -> dict:
    import psutil

    from app.indicators import pipeline
    from app.services import heavy_job_limiter

    gib = 1024**3
    with patch.object(psutil, "virtual_memory", lambda: SimpleNamespace(total=32*gib, available=16*gib)):
        try:
            from app.services import memory_budget
        except ImportError:
            memory_budget = None
        if memory_budget is not None:
            proc, mount = root / "proc", root / "cgroup"
            proc.mkdir()
            mount.mkdir()
            (proc / "cgroup").write_text("0::/\n")
            (proc / "mountinfo").write_text(f"1 0 0:1 / {mount} rw - cgroup2 cgroup rw\n")
            (mount / "memory.max").write_text(str(2*gib))
            (mount / "memory.current").write_text(str(3*gib//2))
            memory_budget._PROC_SELF = proc
        with patch.object(heavy_job_limiter, "_HEAVY_MIN_AVAILABLE_BYTES", gib):
            return {"simulated_host_gib": 32, "container_limit_gib": 2,
                    "container_available_gib": 0.5,
                    "heavy_job_admitted": heavy_job_limiter._memory_admission_ok(),
                    "batch_symbols_for_1500_rows_each": pipeline._adaptive_sym_batch(1000, 1500)}


def main():
    logging.basicConfig(level=logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="tsp-hotpaths-") as temp:
        root = Path(temp)
        os.environ["DATA_DIR"] = str(root / "state")
        os.environ["TICKFLOW_ENV_FILE"] = str(root / "absent.env")
        result = {"ext_query": ext_benchmark(root), "overview": overview_benchmark(),
                  "etf": etf_benchmark(root), "memory": memory_benchmark(root)}
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
