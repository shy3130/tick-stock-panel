"""Pipeline status polling stays available while computations and disk writes wait."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import anyio.to_thread
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import pipeline
from app.services.pipeline_jobs import JobStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline, "job_store", store)
    return store


def test_jobs_respond_while_sync_request_pool_is_saturated(store):
    entered, release = threading.Event(), threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        try:
            yield
        finally:
            limiter.total_tokens = previous

    app = FastAPI(lifespan=lifespan)
    app.include_router(pipeline.router)

    @app.get("/long-compute")
    def long_compute():
        entered.set()
        release.wait(4)
        return {"ok": True}

    jid, _ = store.create(timeout_s=60)
    store.start(jid)
    store.progress(jid, "sync", 57, "working", stage_pct=30)
    with TestClient(app) as client, ThreadPoolExecutor(2) as pool:
        computation = pool.submit(client.get, "/long-compute")
        try:
            assert entered.wait(1)
            response = pool.submit(client.get, "/api/pipeline/jobs?limit=1").result(timeout=0.5)
            assert response.status_code == 200
            body = response.json()
            assert body["active_id"] == jid
            assert [
                (j["id"], j["status"], j["progress"], j["stage_pct"]) for j in body["jobs"]
            ] == [
                (jid, "running", 57, 30),
            ]
            assert not computation.done()
        finally:
            release.set()
            assert computation.result(timeout=2).status_code == 200


def test_jobs_read_memory_after_restart_and_keep_current_progress(store, monkeypatch):
    completed, _ = store.create(timeout_s=60)
    store.start(completed)
    store.succeed(completed, {"minute_rows": 100})
    revived = JobStore(store_dir=store._store_dir)
    active, _ = revived.create(timeout_s=60)
    revived.start(active)
    revived.progress(active, "sync", 67, "working", stage_pct=81)
    monkeypatch.setattr(pipeline, "job_store", revived)

    def forbid_history_scan():
        pytest.fail("polling rescanned job history")

    monkeypatch.setattr(revived, "_job_files_sorted", forbid_history_scan)
    app = FastAPI()
    app.include_router(pipeline.router)
    with TestClient(app) as client:
        for _ in range(3):
            response = client.get("/api/pipeline/jobs")
            assert response.status_code == 200
            jobs = {j["id"]: j for j in response.json()["jobs"]}
            assert response.json()["active_id"] == active
            assert jobs[active]["progress"] == 67
            assert jobs[active]["stage_pct"] == 81
            assert jobs[completed]["result"] == {"minute_rows": 100}


@pytest.mark.parametrize("status", ["pending", "running", "succeeded", "failed"])
def test_jobs_remain_consistent_while_persisting_state(store, monkeypatch, status):
    jid = None
    if status != "pending":
        jid, _ = store.create(timeout_s=60)
        store.start(jid)
        store.progress(jid, "sync", 44, "working")
    entered, release = threading.Event(), threading.Event()
    write = store._write_file

    def slow_write(job):
        entered.set()
        assert release.wait(4)
        write(job)

    monkeypatch.setattr(store, "_write_file", slow_write)
    action = {
        "pending": lambda: store.create(timeout_s=60),
        "running": lambda: store.start(jid),
        "succeeded": lambda: store.succeed(jid, {"daily_rows": 42}),
        "failed": lambda: store.fail(jid, "cancelled"),
    }[status]
    app = FastAPI()
    app.include_router(pipeline.router)
    with TestClient(app) as client, ThreadPoolExecutor(2) as pool:
        persistence = pool.submit(action)
        try:
            assert entered.wait(1)
            response = pool.submit(client.get, "/api/pipeline/jobs").result(timeout=0.5)
            body = response.json()
            (job,) = body["jobs"]
            assert job["status"] == status
            assert body["active_id"] == (job["id"] if status in {"pending", "running"} else None)
            if status == "succeeded":
                assert job["result"] == {"daily_rows": 42}
                assert job["progress"] == 100
            if status == "failed":
                assert job["error"] == "cancelled"
            assert not persistence.done()
        finally:
            release.set()
            persistence.result(timeout=2)


def test_jobs_snapshot_is_bounded_sorted_and_clearable(store):
    # Use distinct times to test ordering independently of timestamp resolution.
    store._max_jobs = 2
    ids = []
    for n in range(3):
        jid, _ = store.create(timeout_s=60)
        store._active_jobs[jid]["started_at"] = (
            datetime.now(UTC) + timedelta(seconds=n)
        ).isoformat()
        store.succeed(jid, {"sequence": n})
        ids.append(jid)
    assert [j["id"] for j in store.list_recent()] == list(reversed(ids[-2:]))
    assert {j["id"] for j in store.list_recent()} == {
        p.stem for p in store._store_dir.glob("*.json")
    }
    assert store.list_recent(limit=1)[0]["id"] == ids[-1]
    row = store.list_recent()[0]
    row["status"] = "pending"
    assert store.list_recent()[0]["status"] == "succeeded"
    store.clear()
    assert store.list_recent() == []
    assert store.active_id() is None


def test_snapshot_loading_ignores_incomplete_or_corrupt_history(store):
    jid, _ = store.create(timeout_s=60)
    store.start(jid)
    store.succeed(jid, {"daily_rows": 123})
    for name, content in {
        "incomplete": '{"id":"old","status":"succeeded"}',
        "corrupt": "{",
        "array": "[]",
    }.items():
        (store._store_dir / f"{name}.json").write_text(content, encoding="utf-8")
    revived = JobStore(store_dir=store._store_dir)
    (job,) = revived.list_recent()
    assert job["id"] == jid
    assert job["result"] == {"daily_rows": 123}


def test_history_retention_follows_deleted_files_even_when_times_differ(store):
    store._max_jobs = 3
    ids = []
    for n in range(3):
        jid, _ = store.create(timeout_s=60)
        store.start(jid)
        store.succeed(jid, {"sequence": n})
        ids.append(jid)
    # Retention uses file mtime, while the UI orders by started_at. A repaired
    # orphan file can have an older start time and a recent modification time.
    os.utime(store._store_dir / f"{ids[1]}.json", (1, 1))
    newest, _ = store.create(timeout_s=60)
    store.start(newest)
    store.succeed(newest, {"sequence": 3})
    assert {j["id"] for j in store.list_recent()} == {ids[0], ids[2], newest}
    assert {j["id"] for j in store.list_recent()} == {
        p.stem for p in store._store_dir.glob("*.json")
    }
