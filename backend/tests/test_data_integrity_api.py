from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import data
from app.services import (
    data_integrity,
    heavy_job_limiter,
    integrity_check,
    pipeline_jobs,
    preferences,
)


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    from app.tickflow.repository import DataStore, KlineRepository
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(data, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", heavy_job_limiter.HeavyJobLimiter())
    monkeypatch.setattr(heavy_job_limiter, "_memory_admission_ok", lambda: True)
    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(integrity_check, "cn_today", lambda: date(2026, 10, 30))
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: {date(2026, 10, 29)})
    paused = []

    @contextmanager
    def pause():
        paused.append("pause")
        try:
            yield
        finally:
            paused.append("resume")

    app = FastAPI()
    app.include_router(data.router)
    app.state.repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(app.state.repo, "get_instruments_asset", lambda asset: pl.DataFrame({"symbol": ["600001.SH"]}))
    app.state.capabilities = SimpleNamespace(has=lambda cap: True)
    app.state.quote_service = SimpleNamespace(paused=pause)
    return app, store, tmp_path, paused


def finished(store, jid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = store.get(jid)
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_api_scan_and_repair_publish_rechecked_report(app_env, monkeypatch):
    from app.services import kline_sync

    app, store, root, paused = app_env
    part = root / "kline_daily/date=2026-10-29"
    part.mkdir(parents=True)
    (part / "part.parquet").write_bytes(b"bad parquet")

    def fetch(*args, **kwargs):
        assert kwargs["asset_type"] == "stock"
        yield pl.DataFrame({
            "symbol": ["600001.SH"], "date": [date(2026, 10, 29)],
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0],
            "volume": [100.0], "amount": [100000.0],
        })

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    with TestClient(app) as client:
        assert client.get("/api/data/integrity").json()["report"] is None
        response = client.post("/api/data/integrity/scan", json={"lookback_days": 7, "asset_types": ["stock"]})
        assert response.status_code == 200
        job = finished(store, response.json()["job_id"])
        assert job["status"] == "succeeded"
        report = client.get("/api/data/integrity").json()["report"]
        assert report["repairable_count"] == 1
        assert paused == []  # 只读扫描不暂停实时行情

        response = client.post("/api/data/integrity/repair", json={"report_id": report["id"]})
        assert response.status_code == 200
        job = finished(store, response.json()["job_id"])
        assert job["status"] == "succeeded"
        final = client.get("/api/data/integrity").json()["report"]
        assert final["status"] == "clean" and final["issues"] == []
        assert paused == ["pause", "resume"]


@pytest.mark.parametrize("body", [
    {"lookback_days": 0}, {"lookback_days": 367}, {"asset_types": []},
    {"asset_types": ["../../outside"]},
])
def test_api_rejects_invalid_scan_scope(app_env, body):
    app, _, _, _ = app_env
    with TestClient(app) as client:
        assert client.post("/api/data/integrity/scan", json=body).status_code == 422


def test_api_refuses_other_active_job_and_stale_report(app_env):
    app, store, _, _ = app_env
    store.create(timeout_s=60)
    with TestClient(app) as client:
        assert client.post("/api/data/integrity/scan", json={}).status_code == 409
        response = client.post("/api/data/integrity/repair", json={"report_id": "f" * 32})
        assert response.status_code == 409 and "重新自检" in response.json()["detail"]


def test_api_empty_scan_does_not_require_data_source(app_env):
    app, store, _, _ = app_env
    app.state.capabilities = SimpleNamespace(has=lambda cap: False)
    with TestClient(app) as client:
        response = client.post("/api/data/integrity/scan", json={"asset_types": ["etf"]})
        assert response.status_code == 200
        assert finished(store, response.json()["job_id"])["status"] == "succeeded"
        report = client.get("/api/data/integrity").json()["report"]
        assert report["status"] == "empty" and report["issues"] == []
        assert report["no_data_assets"] == ["etf"]


def test_api_repair_reports_unavailable_source_without_starting_job(app_env):
    app, store, root, _ = app_env
    part = root / "kline_daily/date=2026-10-29"
    part.mkdir(parents=True)
    (part / "part.parquet").write_bytes(b"bad parquet")
    report = integrity_check.run_check(root, asset_types=["stock"])
    app.state.capabilities = SimpleNamespace(has=lambda cap: False)
    with TestClient(app) as client:
        response = client.post("/api/data/integrity/repair", json={"report_id": report["id"]})
        assert response.status_code == 403 and "数据源" in response.json()["detail"]
        assert store.list_recent() == []
