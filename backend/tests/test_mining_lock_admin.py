"""挖掘锁持有者记录 / 陈旧判定 / 强制释放 + 撞锁降级启动测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mining import router as mining_router
from app.services.mining_lock_admin import (
    MiningLockAdminError,
    _classify,
    _count_active_runs,
    _store_idle_seconds,
    force_release,
    lock_status,
)
from app.services.mining_process_lock import (
    MiningProcessLock,
    MiningProcessLockError,
    probe_lock_held,
    read_lock_holder,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[1]

_HOLDER_SCRIPT = """
import sys, time
from app.services.mining_process_lock import MiningProcessLock
lock = MiningProcessLock(sys.argv[1])
lock.acquire(role="worker")
print("HELD", flush=True)
time.sleep(600)
"""

_POSIX = pytest.mark.skipif(os.name == "nt", reason="外部持锁进程仅在 POSIX 验证")


def _spawn_holder(data_dir: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(_BACKEND_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(data_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(_BACKEND_ROOT),
    )
    line = proc.stdout.readline()
    assert line.strip() == "HELD", f"外部持锁进程未就绪: {line!r} {proc.stderr.read()}"
    return proc


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


# ── 锁内容与探测 ───────────────────────────────────────────────


def test_lock_records_holder_and_release_marker(tmp_path):
    lock = MiningProcessLock(tmp_path)
    lock.acquire()
    holder = read_lock_holder(tmp_path)
    assert holder is not None
    assert holder["pid"] == os.getpid()
    assert holder["role"] == "app"
    assert probe_lock_held(tmp_path) is True

    lock.release()
    assert probe_lock_held(tmp_path) is False
    # 释放标记后不再把内容当作有效持有者
    assert read_lock_holder(tmp_path) is None


def test_second_acquire_conflicts_in_same_process(tmp_path):
    first = MiningProcessLock(tmp_path)
    first.acquire()
    second = MiningProcessLock(tmp_path)
    with pytest.raises(MiningProcessLockError):
        second.acquire()
    first.release()


def test_legacy_lock_content_is_unknown_holder(tmp_path):
    path = tmp_path / ".mining_process.lock"
    path.write_bytes(b"0")
    assert read_lock_holder(tmp_path) is None
    assert probe_lock_held(tmp_path) is False


def test_corrupt_lock_content_is_unknown_holder(tmp_path):
    (tmp_path / ".mining_process.lock").write_text("{not json")
    assert read_lock_holder(tmp_path) is None


def test_status_reports_mine(tmp_path):
    lock = MiningProcessLock(tmp_path)
    lock.acquire()
    try:
        status = lock_status(tmp_path)
        assert status["held"] is True
        assert status["mine"] is True
        assert status["force_releasable"] is False
        assert "当前后端" in status["reason"]
    finally:
        lock.release()


# ── 陈旧判定单元矩阵 ──────────────────────────────────────────


def test_classify_matrix():
    base = dict(
        held=True, mine=False, holder_known=True, holder_alive=True,
        pid_consistent=True, active_runs=0, store_idle=None, stale_after=600.0,
    )
    ok, reason = _classify(**base)
    assert ok and reason is None

    not_held = {**base, "held": False}
    assert _classify(**not_held) == (False, "锁未被持有")

    legacy = {**base, "holder_known": False}
    ok, reason = _classify(**legacy)
    assert not ok and "旧格式" in reason

    reused = {**base, "pid_consistent": False}
    ok, reason = _classify(**reused)
    assert not ok and "复用" in reason

    active = {**base, "active_runs": 2}
    ok, reason = _classify(**active)
    assert not ok and "2 个进行中" in reason

    fresh = {**base, "store_idle": 12.0}
    ok, reason = _classify(**fresh)
    assert not ok and "陈旧阈值" in reason

    dead = {**base, "holder_alive": False}
    ok, reason = _classify(**dead)
    assert not ok and "已不存在" in reason


def test_count_active_runs_and_store_idle(tmp_path):
    runs = tmp_path / "research" / "mining" / "runs"
    assert _count_active_runs(tmp_path) == 0
    assert _store_idle_seconds(tmp_path, now=time.time()) is None

    done = runs / "run-done"
    done.mkdir(parents=True)
    (done / "manifest.json").write_text(json.dumps({"status": "succeeded"}))
    active = runs / "run-live"
    active.mkdir(parents=True)
    (active / "manifest.json").write_text(json.dumps({"status": "running"}))
    (active / "events.jsonl").write_text("{}\n")

    assert _count_active_runs(tmp_path) == 1
    idle = _store_idle_seconds(tmp_path, now=time.time())
    assert idle is not None and idle < 60


def test_count_active_runs_tolerates_corrupt_manifest(tmp_path):
    runs = tmp_path / "research" / "mining" / "runs" / "bad"
    runs.mkdir(parents=True)
    (runs / "manifest.json").write_text("{broken")
    assert _count_active_runs(tmp_path) == 0


# ── 强制释放 (真实外部持有进程, POSIX) ─────────────────────────


@_POSIX
def test_force_release_kills_external_holder_and_takes_over(tmp_path):
    proc = _spawn_holder(tmp_path)
    try:
        status = lock_status(tmp_path)
        assert status["held"] is True
        assert status["mine"] is False
        assert status["holder"]["role"] == "worker"
        assert status["force_releasable"] is True, status["reason"]

        lock = MiningProcessLock(tmp_path)
        report = force_release(tmp_path, lock)
        assert report["released"] is True
        assert report["terminated_pid"] == proc.pid
        assert report["reacquired"] is True
        assert lock.acquired is True
        proc.wait(timeout=10)
        assert lock_status(tmp_path)["mine"] is True
        lock.release()
    finally:
        _kill(proc)


@_POSIX
def test_force_release_refuses_fresh_store(tmp_path):
    proc = _spawn_holder(tmp_path)
    try:
        runs = tmp_path / "research" / "mining" / "runs" / "r1"
        runs.mkdir(parents=True)
        (runs / "manifest.json").write_text(json.dumps({"status": "succeeded"}))
        status = lock_status(tmp_path)
        assert status["force_releasable"] is False
        assert "陈旧阈值" in status["reason"]
        with pytest.raises(MiningLockAdminError, match="陈旧阈值"):
            force_release(tmp_path, MiningProcessLock(tmp_path))
        assert proc.poll() is None, "被拒绝时不得误杀持有者"
    finally:
        _kill(proc)


@_POSIX
def test_force_release_refuses_active_runs(tmp_path):
    proc = _spawn_holder(tmp_path)
    try:
        runs = tmp_path / "research" / "mining" / "runs" / "r2"
        runs.mkdir(parents=True)
        (runs / "manifest.json").write_text(json.dumps({"status": "running"}))
        status = lock_status(tmp_path)
        assert status["force_releasable"] is False
        assert "进行中" in status["reason"]
        assert proc.poll() is None
    finally:
        _kill(proc)


def test_force_release_refuses_when_lock_free(tmp_path):
    with pytest.raises(MiningLockAdminError, match="未被持有"):
        force_release(tmp_path, MiningProcessLock(tmp_path))


# ── API 接线: 撞锁降级 + 锁端点 ────────────────────────────────


def _api_client(tmp_path, *, acquired: bool):
    app = FastAPI()
    app.include_router(mining_router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.mining_lock_acquired = acquired
    app.state.mining_process_lock = MiningProcessLock(tmp_path)
    return TestClient(app)


def test_degraded_mode_rejects_mining_start(tmp_path):
    client = _api_client(tmp_path, acquired=False)
    resp = client.post(
        "/api/backtest/mining/runs",
        json={"factor_names": ["momentum_5d"], "budget_profile": "exploratory"},
    )
    assert resp.status_code == 409
    assert "降级" in resp.json()["detail"]


def test_lock_endpoints_free_lock(tmp_path):
    client = _api_client(tmp_path, acquired=True)
    body = client.get("/api/backtest/mining/lock").json()
    assert body["held"] is False
    assert body["force_releasable"] is False
    resp = client.post("/api/backtest/mining/lock/force-release")
    assert resp.status_code == 409
    assert "未被持有" in resp.json()["detail"]


@_POSIX
def test_lock_endpoints_full_recovery_loop(tmp_path):
    proc = _spawn_holder(tmp_path)
    try:
        client = _api_client(tmp_path, acquired=False)
        body = client.get("/api/backtest/mining/lock").json()
        assert body["held"] is True
        assert body["mine"] is False
        assert body["force_releasable"] is True, body["reason"]

        resp = client.post("/api/backtest/mining/lock/force-release")
        assert resp.status_code == 200
        assert resp.json() == {
            "released": True,
            "terminated_pid": proc.pid,
            "reacquired": True,
        }
        proc.wait(timeout=10)
        after = client.get("/api/backtest/mining/lock").json()
        assert after["mine"] is True

        # 接管后挖掘入口必须立即恢复 — 不再是 409 降级
        # (stub app 无 manager, 返回 503; 真实环境此处应为 200/400)
        resp = client.post(
            "/api/backtest/mining/runs",
            json={"factor_names": ["momentum_5d"], "budget_profile": "exploratory"},
        )
        assert resp.status_code != 409
        assert "降级" not in resp.json().get("detail", "")
    finally:
        _kill(proc)
