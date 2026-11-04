"""HTTP load scenarios with isolated storage and a counted synthetic strategy engine.

These validate orchestration, not financial formulas or production user capacity.
Run with -s to collect request counts and latency evidence.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import data, screener
from app.main import auth_middleware, shared_work_busy_handler, workload_middleware
from app.services import strategy_run_queue
from app.services import workload as metrics
from app.services.shared_work import WorkBusyError


@dataclass
class Result:
    total: int = 1
    rows: list = field(default_factory=lambda: [{"symbol": "000001.SZ", "close": 10.0}])


class Engine:
    def __init__(self):
        self._strategies = {str(i): SimpleNamespace(meta={}) for i in range(10)}
        self.calls = []
        self.release = None

    def has(self, sid):
        return sid in self._strategies

    def get(self, sid):
        return self._strategies[sid]

    def run_all(self, context, *, strategy_ids, **kwargs):
        self.calls.extend(strategy_ids)
        if self.release is not None:
            assert self.release.wait(5)
        time.sleep(0.01)
        return {sid: Result() for sid in strategy_ids}

    def run(self, strategy_id, context, **kwargs):
        self.calls.append(strategy_id)
        time.sleep(0.01)
        return Result()


class Service:
    def __init__(self, *args, **kwargs):
        pass

    def latest_date(self):
        return date(2026, 11, 4)

    def build_strategy_context(self, *args, **kwargs):
        return SimpleNamespace()


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = Engine()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    manager = strategy_run_queue.StrategyRunManager(max_pending=2)
    monkeypatch.setattr(strategy_run_queue, "MANAGER", manager)
    monkeypatch.setattr(screener, "ScreenerService", Service)
    monkeypatch.setattr(metrics, "submissions", metrics.SubmissionLimiter(limit=30))
    app = FastAPI()
    app.middleware("http")(workload_middleware)
    app.add_exception_handler(WorkBusyError, shared_work_busy_handler)
    app.include_router(screener.router)
    app.include_router(data.router)
    app.state.repo, app.state.strategy_engine = repo, engine

    @app.get("/health")
    async def health():
        return {"ok": True}

    with TestClient(app) as http:
        yield http, engine, manager
    if engine.release is not None:
        engine.release.set()


def body(*, ensure=True, sid="0"):
    return {"as_of": "2026-11-04", "strategy_ids": [sid], "summary_only": True, "ensure": ensure}


@pytest.mark.parametrize("clients", [1, 10, 30, 100])
def test_shared_page_load_and_polling_do_not_repeat_work(client, clients):
    http, engine, _ = client
    barrier = threading.Barrier(clients)

    def visit(_):
        barrier.wait(10)
        started = time.perf_counter()
        response = http.post("/api/screener/run_all", json=body())
        assert response.status_code == 200, response.text
        return response.json(), (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(clients) as pool:
        responses = list(pool.map(visit, range(clients)))
    assert engine.calls == ["0"]
    task_id = responses[0][0]["task_id"]
    assert all(result["task_id"] == task_id for result, _ in responses)
    for _ in range(10):
        response = http.get("/api/screener/cached-summary", params={"task_id": task_id})
        assert response.status_code == 200
        assert response.json()["task"]["complete"]
    assert engine.calls == ["0"]
    # Sequential later visitors share the completed result as well.
    assert http.post("/api/screener/run_all", json=body()).json()["task_id"] == task_id
    latencies = sorted(ms for _, ms in responses)
    print(json.dumps({"clients": clients, "autoload_requests": clients + 1, "status_polls": 10,
                      "strategy_executions": len(engine.calls),
                      "p95_ms": round(latencies[min(len(latencies) - 1, int(len(latencies) * .95))], 2)}))


def test_completed_manual_runs_keep_legacy_rerun_behavior(client):
    http, engine, _ = client
    for _ in range(3):
        assert http.post("/api/screener/run_all", json=body(ensure=False)).status_code == 200
    assert engine.calls == ["0", "0", "0"]
    print(json.dumps({"legacy_sequential_requests": 3, "strategy_executions": 3}))


def test_distinct_heavy_requests_are_bounded_and_health_stays_available(client):
    http, engine, manager = client
    engine.release = threading.Event()
    try:
        with ThreadPoolExecutor(10) as pool:
            responses = list(pool.map(lambda i: http.post("/api/screener/run_all", json=body(sid=str(i))), range(10)))
        assert sum(response.status_code == 200 for response in responses) == 2
        busy = [response for response in responses if response.status_code == 503]
        assert len(busy) == 8
        assert all(response.headers["Retry-After"] == "3" for response in busy)
        assert manager.snapshot()["pending"] == 2
        assert http.get("/health").status_code == 200
        assert http.get("/api/data/workload").status_code == 200
    finally:
        engine.release.set()
    deadline = time.monotonic() + 5
    while manager.snapshot()["pending"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.snapshot()["pending"] == 0


def test_shared_completed_autoloads_do_not_consume_submission_budget(client, monkeypatch):
    http, engine, _ = client
    monkeypatch.setattr(metrics, "submissions", metrics.SubmissionLimiter(limit=1))
    http.cookies.set("tf_session", "isolated-test-session")
    for _ in range(5):
        assert http.post("/api/screener/run_all", json=body()).status_code == 200
    assert engine.calls == ["0"]
    response = http.post("/api/screener/run_all", json=body(sid="1"))
    assert response.status_code == 503


def test_preset_views_share_results_and_manual_rerun_bypasses_completed_cache(client):
    http, engine, _ = client
    request = {"strategy_id": "0", "as_of": "2026-11-04", "ensure": True}
    with ThreadPoolExecutor(20) as pool:
        responses = list(pool.map(lambda _: http.post("/api/screener/run_preset", json=request), range(20)))
    assert all(response.status_code == 200 for response in responses)
    assert engine.calls == ["0"]
    assert http.post("/api/screener/run_preset", json=request).status_code == 200
    assert engine.calls == ["0"]
    assert http.post("/api/screener/run_preset", json={**request, "ensure": False}).status_code == 200
    assert engine.calls == ["0", "0"]


def test_workload_diagnostics_inherit_panel_authentication(client, monkeypatch):
    from app.services import auth

    http, _, _ = client
    app = FastAPI()
    app.middleware("http")(auth_middleware)
    app.include_router(data.router)
    app.state.repo = http.app.state.repo
    monkeypatch.setattr(auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth, "is_valid_session", lambda token: token == "valid-test-session")
    with TestClient(app) as protected:
        assert protected.get("/api/data/workload").status_code == 401
        protected.cookies.set("tf_session", "valid-test-session")
        assert protected.get("/api/data/workload").status_code == 200
