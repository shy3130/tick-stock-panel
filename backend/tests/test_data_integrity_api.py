from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._platform_caps import skip_no_atomic_replace

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
    # 15s 而非 3s: 修复任务要逐个分区写 parquet, Windows 上目标文件仍被读句柄持有
    # 时 replace 会走短退避重试, 单日就可能耗掉数百毫秒, 队列一长 3s 必然不够。
    # 这里等的是「任务是否会完成」, 不是「多快完成」, 放宽窗口不降低验证强度。
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = store.get(jid)
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.01)
    job = store.get(jid)
    raise AssertionError(f"job did not finish: status={job and job.get('status')} job={job}")


@skip_no_atomic_replace
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


def test_api_repair_reports_old_history_and_next_attempt_repairs_it(app_env, monkeypatch):
    from app.services import daily_health, kline_sync

    app, store, root, paused = app_env
    old, recent = date(2026, 8, 31), date(2026, 10, 29)
    symbol = "600001.SH"
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: {old, recent})

    def frame(day, bad=False):
        return pl.DataFrame({
            "symbol": [symbol], "date": [day], "open": [10.0], "high": [11.0],
            "low": [9.0], "close": [999.0 if bad else 10.0],
            "volume": [100.0], "amount": [1000.0],
        })

    for day in (old, recent):
        app.state.repo.append_daily(frame(day, bad=day == old))
        app.state.repo.append_enriched(frame(day))
    issue = data_integrity.IntegrityIssue(recent, "kline_daily", "invalid", symbols=(symbol,))
    daily_health.record_scan(root, [issue], assets=["stock"], start=recent, end=recent)
    daily_health.record_scan(root, [], assets=["stock"], start=recent, end=recent)
    fetched = []

    def fetch(symbols, _caps, **kwargs):
        fetched.append((kwargs["start_time"].date(), symbols))
        yield frame(old)

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    with TestClient(app) as client:
        response = client.post("/api/data/integrity/scan", json={"lookback_days": 30, "asset_types": ["stock"]})
        assert finished(store, response.json()["job_id"])["status"] == "succeeded"
        report = client.get("/api/data/integrity").json()["report"]
        response = client.post("/api/data/integrity/repair", json={"report_id": report["id"]})
        failure = finished(store, response.json()["job_id"])
        assert failure["status"] == "failed"
        assert "2026-08-31" in failure["error"] and symbol in failure["error"]
        result = client.get("/api/data/integrity").json()
        assert result["blocking_assets"] == ["stock"]
        historical = next(i for i in result["report"]["issues"] if i["date"] == old.isoformat())
        assert historical["symbols"] == [symbol] and "最高价" in historical["reason"]
        assert fetched == []
        response = client.post("/api/data/integrity/repair", json={"report_id": result["report"]["id"]})
        assert finished(store, response.json()["job_id"])["status"] == "succeeded"
        final = client.get("/api/data/integrity").json()
        assert final["report"]["status"] == "clean" and final["blocking_assets"] == []
        assert final["report"]["lookback_days"] == 30
        assert fetched == [(old, [symbol])]
        assert paused == ["pause", "resume", "pause", "resume"]


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
