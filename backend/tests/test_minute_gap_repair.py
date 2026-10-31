"""分钟缺口一键补拉: 定向批量、任务互斥、失败与取消后的可见性。"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline
from app.services import heavy_job_limiter, pipeline_jobs, preferences
from app.services.heavy_job_limiter import HeavyJobLimiter
from app.services.pipeline_jobs import JobStore


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("分钟补拉任务未在测试期限内完成")


@pytest.fixture
def repair_env(monkeypatch, tmp_path):
    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    store = JobStore(store_dir=tmp_path / "jobs")
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.005)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", limiter)
    monkeypatch.setattr(kline, "_minute_allowed", lambda _: True)
    sync = MagicMock(return_value=480)
    refresh = MagicMock()
    invalidate = MagicMock()
    pool = MagicMock(return_value=["600519.SH"])
    monkeypatch.setattr(kline.kline_sync, "sync_and_persist_minute", sync)
    monkeypatch.setattr("app.jobs.daily_pipeline._refresh_single_view", refresh)
    monkeypatch.setattr("app.api.data.invalidate_storage_cache", invalidate)
    monkeypatch.setattr("app.tickflow.pools.get_pool", pool)
    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda symbol: {"000001.SH": "index", "510300.SH": "etf"}.get(symbol, "stock"),
        get_index_symbol_set=lambda: {"000001.SH"},
    )
    app.state.capabilities = object()
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, store=store, limiter=limiter, repo=app.state.repo,
            sync=sync, refresh=refresh, invalidate=invalidate, pool=pool,
        )


def test_repair_deduplicates_symbols_and_forces_historical_window(repair_env):
    env = repair_env
    response = env.client.post("/api/kline/repair_minute_gaps", json={
        "symbols": ["600000.SH", " 000001.SZ ", "600000.SH"],
    })
    assert response.status_code == 200, response.text
    result = response.json()
    wait_for(lambda: env.store.get(result["job_id"])["status"] == "succeeded")
    args, kwargs = env.sync.call_args
    assert args[0] == ["600000.SH", "000001.SZ"]
    assert kwargs["days"] == 10
    assert kwargs["force_full_days"] is True
    assert kwargs["extend_backward"] is False
    assert env.store.get(result["job_id"])["result"] == {"minute_rows": 480, "universe_size": 2}
    env.pool.assert_not_called()
    env.refresh.assert_called_once_with(env.repo, "kline_minute")
    env.invalidate.assert_called()


@pytest.mark.parametrize("symbols", [None, [], "600000.SH", [None], [""], [" "], [123],
                                         ["600000.SH"] * 2001, ["000001.SH"], ["510300.SH"]])
def test_repair_rejects_invalid_scope_before_creating_job(repair_env, symbols):
    env = repair_env
    response = env.client.post("/api/kline/repair_minute_gaps", json={"symbols": symbols})
    assert response.status_code == 400
    env.sync.assert_not_called()
    assert env.store.active_id() is None


def test_repair_requires_usable_minute_provider(repair_env, monkeypatch):
    monkeypatch.setattr(kline, "_minute_allowed", lambda _: False)
    response = repair_env.client.post("/api/kline/repair_minute_gaps", json={"symbols": ["600000.SH"]})
    assert response.status_code == 403
    assert "数据源" in response.json()["detail"]
    repair_env.sync.assert_not_called()


def test_repair_does_not_reuse_unrelated_pending_job(repair_env):
    env = repair_env
    existing_id, _ = env.store.create()
    response = env.client.post("/api/kline/repair_minute_gaps", json={"symbols": ["600000.SH"]})
    assert response.status_code == 409
    assert env.store.active_id() == existing_id
    env.sync.assert_not_called()


def test_failed_repair_refreshes_partial_data_and_reports_error(repair_env):
    env = repair_env
    env.sync.side_effect = ValueError("分钟数据源连接中断")
    response = env.client.post("/api/kline/repair_minute_gaps", json={"symbols": ["600000.SH"]})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    wait_for(lambda: env.store.get(job_id)["status"] == "failed")
    assert "分钟数据源连接中断" in env.store.get(job_id)["error"]
    env.refresh.assert_called_once_with(env.repo, "kline_minute")
    env.invalidate.assert_called()
    wait_for(lambda: env.limiter.in_use == 0)


def test_zero_rows_are_reported_without_claiming_gaps_fixed(repair_env):
    env = repair_env
    env.sync.return_value = 0
    response = env.client.post("/api/kline/repair_minute_gaps", json={"symbols": ["600000.SH"]})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    wait_for(lambda: env.store.get(job_id)["status"] == "succeeded")
    job = env.store.get(job_id)
    assert job["result"]["minute_rows"] == 0
    assert "补齐成功" not in str(job["log"])


def test_cancelled_repair_refreshes_partial_data_and_releases_capacity(repair_env):
    env = repair_env
    entered, finish = threading.Event(), threading.Event()

    def sync(*args, **kwargs):
        entered.set()
        assert finish.wait(3)
        kwargs["on_chunk_done"](1, 2, "最近窗口")
        raise AssertionError("取消后不得继续拉取")

    env.sync.side_effect = sync
    response = env.client.post("/api/kline/repair_minute_gaps", json={"symbols": ["600000.SH"]})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    try:
        assert entered.wait(1)
        env.store.terminate(job_id, "用户停止分钟补拉")
    finally:
        finish.set()
    wait_for(lambda: env.limiter.in_use == 0)
    assert env.store.get(job_id)["status"] == "failed"
    assert "用户停止" in env.store.get(job_id)["error"]
    assert env.store.get(job_id)["stage"] == "sync_minute"
    env.refresh.assert_called_once_with(env.repo, "kline_minute")
