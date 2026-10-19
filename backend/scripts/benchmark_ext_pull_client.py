"""Compare per-request HTTP clients with one client per pull using a mock server.

Run: uv run --frozen python -m scripts.benchmark_ext_pull_client
No external network requests or user data are used.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from datetime import date

import httpx

from app.services import ext_pull
from app.services.ext_data import ExtConfig, PullConfig

PAGES = 30
RUNS = 5


async def run(shared: bool) -> tuple[float, int, int]:
    clients = 0
    requests = 0
    real_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"symbol": "000001.SZ"}] if page <= PAGES else [])

    def factory(*args, **kwargs):
        nonlocal clients
        clients += 1
        return real_client(*args, transport=httpx.MockTransport(respond), **kwargs)

    original = ext_pull.httpx.AsyncClient
    ext_pull.httpx.AsyncClient = factory
    config = ExtConfig(
        id="benchmark", label="benchmark", mode="timeseries", fields=[],
        pull=PullConfig(url="https://example.test/rank", page_param="page", max_pages=PAGES + 1),
    )
    try:
        started = time.perf_counter()
        if shared:
            rows = await ext_pull.fetch_rows_for_date(config, date(2026, 9, 1))
        else:
            for page in range(1, PAGES + 2):
                payload = await ext_pull._request_json(config.pull, config.id, extra_params={"page": str(page)})
                if not payload:
                    break
                if page == 1:
                    rows = []
                rows.extend(payload)
        elapsed = time.perf_counter() - started
        assert len(rows) == PAGES and requests == PAGES + 1
        return elapsed, clients, requests
    finally:
        ext_pull.httpx.AsyncClient = original


async def main() -> None:
    timings: dict[bool, list[float]] = {False: [], True: []}
    for _ in range(RUNS):
        for shared in (False, True):
            elapsed, clients, requests = await run(shared)
            timings[shared].append(elapsed)
            print(f"{'shared' if shared else 'per-request'}: {elapsed:.4f}s, {requests} requests, {clients} clients")
    print(f"median: per-request={statistics.median(timings[False]):.4f}s, shared={statistics.median(timings[True]):.4f}s")


if __name__ == "__main__":
    asyncio.run(main())
