"""挖掘锁持有者记录 / 陈旧判定 / 强制释放 + 撞锁降级启动测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
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
    locate_lock_holder_pid,
    probe_lock_held,
    read_lock_holder,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[1]

_HOLDER_SCRIPT = """
import os, sys, time
from app.services.mining_process_lock import MiningProcessLock
lock = MiningProcessLock(sys.argv[1])
lock.acquire(role="worker")
print(f"HELD {os.getpid()}", flush=True)
time.sleep(600)
"""

# 旧版 (v0.2 时代) 锁写入格式: 只有 b"0", 不含 pid — 升级前启动的残留进程
# 就是这样持锁的, 新版面板无法从内容定位持有者。
_HOLDER_SCRIPT_LEGACY = """
import fcntl, sys, time
path = sys.argv[1] + "/.mining_process.lock"
stream = open(path, "a+b")
stream.write(b"0")
stream.flush()
fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
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
    assert line.startswith("HELD "), f"外部持锁进程未就绪: {line!r} {proc.stderr.read()}"
    # Windows venv launchers may have a different PID from the Python process
    # executing the script. Verify termination against the script's own identity.
    proc.holder_process = psutil.Process(int(line.split()[1]))
    return proc


def _spawn_legacy_holder(data_dir: Path) -> subprocess.Popen:
    """旧格式 (b"0") 持锁进程: 模拟升级前启动、升级后仍残留的旧版后端。"""
    env = {**os.environ, "PYTHONPATH": str(_BACKEND_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT_LEGACY, str(data_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(_BACKEND_ROOT),
    )
    line = proc.stdout.readline()
    assert line.strip() == "HELD", f"旧格式持锁进程未就绪: {line!r} {proc.stderr.read()}"
    proc.holder_process = psutil.Process(proc.pid)
    return proc


def _kill(proc: subprocess.Popen) -> None:
    # Keep the original Process object so cleanup also checks for PID reuse.
    with suppress(psutil.NoSuchProcess):
        proc.holder_process.kill()
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
    assert not ok and "无法确认占用进程" in reason

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
        assert report["terminated_pid"] == proc.holder_process.pid
        assert report["reacquired"] is True
        assert lock.acquired is True
        proc.wait(timeout=10)
        assert lock_status(tmp_path)["mine"] is True
        lock.release()
    finally:
        _kill(proc)


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
    proc = _spawn_holder(tmp_path)
    try:
        resp = client.post(
            "/api/backtest/mining/runs",
            json={"factor_names": ["momentum_5d"], "budget_profile": "exploratory"},
        )
        assert resp.status_code == 409
        assert "降级" in resp.json()["detail"]
        assert proc.poll() is None
    finally:
        _kill(proc)


def test_lock_endpoints_free_lock(tmp_path):
    client = _api_client(tmp_path, acquired=True)
    body = client.get("/api/backtest/mining/lock").json()
    assert body["held"] is False
    assert body["force_releasable"] is False
    resp = client.post("/api/backtest/mining/lock/force-release")
    assert resp.status_code == 409
    assert "未被持有" in resp.json()["detail"]


def test_lock_endpoint_recognizes_own_lock_when_metadata_read_is_denied(tmp_path, monkeypatch):
    """Windows forbids a second handle from reading the locked first byte."""
    client = _api_client(tmp_path, acquired=True)
    lock = client.app.state.mining_process_lock
    lock.acquire()
    original_read = Path.read_bytes

    def read_bytes(path):
        if path == lock.path:
            raise PermissionError("locked byte is unreadable through another handle")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr("app.services.mining_lock_admin.locate_lock_holder_pid", lambda _: None)
    try:
        body = client.get("/api/backtest/mining/lock").json()
        assert body["held"] is True
        assert body["mine"] is True
        assert body["holder"]["pid"] == os.getpid()
        assert body["force_releasable"] is False
        assert "当前后端" in body["reason"]
        refused = client.post("/api/backtest/mining/lock/force-release")
        assert refused.status_code == 409
        assert "当前后端" in refused.json()["detail"]
        assert lock.acquired is True
    finally:
        lock.release()


def test_lock_endpoint_does_not_treat_startup_flag_as_current_ownership(tmp_path, monkeypatch):
    client = _api_client(tmp_path, acquired=True)
    monkeypatch.setattr("app.services.mining_lock_admin.probe_lock_held", lambda _: True)
    monkeypatch.setattr("app.services.mining_lock_admin.read_lock_holder", lambda _: None)
    monkeypatch.setattr("app.services.mining_lock_admin.locate_lock_holder_pid", lambda _: None)
    body = client.get("/api/backtest/mining/lock").json()
    assert body["held"] is True
    assert body["mine"] is False
    assert body["holder"] is None
    assert body["force_releasable"] is False


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
            "terminated_pid": proc.holder_process.pid,
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
        client.app.state.mining_process_lock.release()
        _kill(proc)


# ── 旧格式锁的 OS 层面持有者定位 (用户反馈: 升级后挖掘一直不可用) ──────────


@_POSIX
def test_legacy_lock_holder_located_via_os_scan(tmp_path):
    """旧格式锁 (b"0", 无 pid) 也能从系统层面定位持有者。

    用户场景: 升级前启动的旧版后端进程残留 (仍持有 flock, 内容为旧格式),
    新版面板无法从内容定位 → 一直提示"请手动结束后端残留进程"。
    """
    proc = _spawn_legacy_holder(tmp_path)
    try:
        assert read_lock_holder(tmp_path) is None
        assert probe_lock_held(tmp_path) is True

        located = locate_lock_holder_pid(tmp_path)
        assert located == proc.pid

        status = lock_status(tmp_path)
        assert status["held"] is True
        assert status["holder_source"] == "os-scan"
        assert status["holder"] is not None
        assert status["holder"]["pid"] == proc.pid
        assert status["holder_alive"] is True
        assert status["force_releasable"] is True, status["reason"]
    finally:
        _kill(proc)


@_POSIX
def test_legacy_lock_holder_force_release_takes_over(tmp_path):
    """定位到旧格式持有者后, 既有的人工确认强制释放流程可完整恢复挖掘。"""
    proc = _spawn_legacy_holder(tmp_path)
    try:
        lock = MiningProcessLock(tmp_path)
        report = force_release(tmp_path, lock)
        assert report["released"] is True
        assert report["terminated_pid"] == proc.pid
        assert report["reacquired"] is True
        proc.wait(timeout=10)
        assert lock_status(tmp_path)["mine"] is True
        lock.release()
    finally:
        _kill(proc)


@_POSIX
def test_legacy_lock_holder_still_refused_while_mining_active(tmp_path):
    """fail-closed 不放宽: 定位到持有者但仍有进行中挖掘运行/存储刚写入 → 拒绝。"""
    proc = _spawn_legacy_holder(tmp_path)
    try:
        runs = tmp_path / "research" / "mining" / "runs" / "r1"
        runs.mkdir(parents=True)
        (runs / "manifest.json").write_text('{"status": "running"}', encoding="utf-8")

        status = lock_status(tmp_path)
        assert status["holder_source"] == "os-scan"
        assert status["force_releasable"] is False
        assert "进行中" in (status["reason"] or "")
    finally:
        _kill(proc)


def test_locate_returns_none_when_lock_free(tmp_path):
    """锁未被持有 (无进程打开) 时定位返回 None, 不误报持有者。"""
    (tmp_path / ".mining_process.lock").write_bytes(b"0")
    assert locate_lock_holder_pid(tmp_path) is None


def test_pid_probe_is_read_only(monkeypatch):
    from app.services import mining_lock_admin as admin

    def forbidden_signal(*args):
        raise AssertionError("checking a process must not send a Windows console signal")

    # Replace only this module's os reference, leaving psutil's platform backend intact.
    monkeypatch.setattr(admin, "os", SimpleNamespace(kill=forbidden_signal, getpid=os.getpid))
    assert admin._pid_alive(os.getpid()) is True
    assert admin._pid_alive(None) is False
    assert admin._pid_alive(-1) is False


def test_recover_endpoint_acquires_free_lock_and_recovers_orphans_once(tmp_path):
    from app.services.mining_manager import MiningJobManager

    client = _api_client(tmp_path, acquired=False)
    manager = MiningJobManager(tmp_path)
    client.app.state.mining_manager = manager
    manager.store.create({}, "fixture", run_id="orphan")
    manager.store.transition_status("orphan", "running")
    artifact = manager.store.artifact_path("orphan", "factors")
    artifact.write_bytes(b"existing artifact must be preserved")
    lock = client.app.state.mining_process_lock
    try:
        response = client.post("/api/backtest/mining/lock/recover")
        assert response.status_code == 200
        assert response.json()["mine"] is True
        assert client.app.state.mining_lock_acquired is True
        assert manager.store.get("orphan")["status"] == "interrupted"
        assert artifact.read_bytes() == b"existing artifact must be preserved"
        manager.store.create({}, "fixture", run_id="new_live_run")
        manager.store.transition_status("new_live_run", "running")
        assert client.post("/api/backtest/mining/lock/recover").json()["mine"] is True
        assert manager.store.get("new_live_run")["status"] == "running"
    finally:
        lock.release()


def test_recover_repairs_flag_for_own_lock_without_touching_runs(tmp_path):
    client = _api_client(tmp_path, acquired=False)
    manager = Mock()
    client.app.state.mining_manager = manager
    lock = client.app.state.mining_process_lock
    lock.acquire()
    try:
        response = client.post("/api/backtest/mining/lock/recover")
        assert response.status_code == 200
        assert response.json()["mine"] is True
        assert client.app.state.mining_lock_acquired is True
        manager.recover_interrupted.assert_not_called()
    finally:
        lock.release()


def test_recover_refuses_live_owner_then_restores_after_exit(tmp_path):
    from app.services.mining_manager import MiningJobManager

    proc = _spawn_holder(tmp_path)
    client = _api_client(tmp_path, acquired=False)
    manager = MiningJobManager(tmp_path)
    client.app.state.mining_manager = manager
    manager.store.create({}, "fixture", run_id="external_live")
    manager.store.transition_status("external_live", "running")
    manifest = manager.store.runs_root / "external_live" / "manifest.json"
    before = manifest.read_bytes()
    try:
        response = client.post("/api/backtest/mining/lock/recover")
        assert response.status_code == 200
        assert response.json()["held"] is True
        assert response.json()["mine"] is False
        assert response.json()["force_releasable"] is False
        cancel = client.post("/api/backtest/mining/runs/external_live/cancel")
        assert cancel.status_code == 409
        assert manifest.read_bytes() == before
        assert proc.poll() is None
        _kill(proc)
        assert client.get("/api/backtest/mining/lock").json()["held"] is False
        response = client.post("/api/backtest/mining/lock/recover")
        assert response.status_code == 200
        assert response.json()["mine"] is True
        assert manager.store.get("external_live")["status"] == "interrupted"
    finally:
        client.app.state.mining_process_lock.release()
        _kill(proc)


def test_mining_start_reacquires_lock_after_owner_exits(tmp_path):
    proc = _spawn_holder(tmp_path)
    client = _api_client(tmp_path, acquired=False)
    try:
        _kill(proc)
        response = client.post(
            "/api/backtest/mining/runs",
            json={"factor_names": ["momentum_5d"], "budget_profile": "exploratory"},
        )
        assert response.status_code != 409
        assert client.app.state.mining_lock_acquired is True
        assert client.app.state.mining_process_lock.acquired is True
    finally:
        client.app.state.mining_process_lock.release()
        _kill(proc)


def test_concurrent_recovery_only_recovers_once(tmp_path):
    from app.services import mining_lock_admin as admin

    state = _api_client(tmp_path, acquired=False).app.state
    manager = Mock()
    manager.recover_interrupted.return_value = 0
    state.mining_manager = manager
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: admin.ensure_mining_lock(state), range(8)))
        assert all(results)
        manager.recover_interrupted.assert_called_once_with()
        assert state.mining_lock_acquired is True
    finally:
        state.mining_process_lock.release()


def test_recovery_failure_releases_lock_and_keeps_mining_disabled(tmp_path):
    from app.services.mining_jobs import MiningRunStoreError

    client = _api_client(tmp_path, acquired=False)
    manager = Mock()
    manager.recover_interrupted.side_effect = MiningRunStoreError("cannot read runs")
    client.app.state.mining_manager = manager
    response = client.post("/api/backtest/mining/lock/recover")
    assert response.status_code == 409
    assert client.app.state.mining_lock_acquired is False
    assert client.app.state.mining_process_lock.acquired is False
    assert probe_lock_held(tmp_path) is False


def test_recovery_permission_failure_is_explicit(tmp_path, monkeypatch):
    client = _api_client(tmp_path, acquired=False)
    lock = client.app.state.mining_process_lock
    monkeypatch.setattr(lock, "acquire", Mock(side_effect=PermissionError("denied")))
    response = client.post("/api/backtest/mining/lock/recover")
    assert response.status_code == 409
    assert "权限" in response.json()["detail"]
    assert lock.acquired is False
    assert client.app.state.mining_lock_acquired is False


def test_force_release_rechecks_confirmed_pid(tmp_path):
    proc = _spawn_holder(tmp_path)
    client = _api_client(tmp_path, acquired=False)
    try:
        response = client.post(
            "/api/backtest/mining/lock/force-release",
            json={"expected_pid": proc.holder_process.pid + 1},
        )
        assert response.status_code == 409
        assert "持有者已变化" in response.json()["detail"]
        assert proc.poll() is None
        assert client.app.state.mining_process_lock.acquired is False
    finally:
        _kill(proc)


def test_unknown_owner_recovery_never_terminates_process(tmp_path, monkeypatch):
    proc = _spawn_holder(tmp_path)
    client = _api_client(tmp_path, acquired=False)
    monkeypatch.setattr("app.services.mining_lock_admin.read_lock_holder", lambda _: None)
    monkeypatch.setattr("app.services.mining_lock_admin.locate_lock_holder_pid", lambda _: None)
    try:
        response = client.post("/api/backtest/mining/lock/recover")
        assert response.status_code == 200
        assert response.json()["holder"] is None
        assert response.json()["force_releasable"] is False
        assert "无法确认" in response.json()["reason"]
        assert client.post("/api/backtest/mining/lock/force-release").status_code == 409
        assert proc.poll() is None
        _kill(proc)
        assert client.post("/api/backtest/mining/lock/recover").json()["mine"] is True
    finally:
        client.app.state.mining_process_lock.release()
        _kill(proc)


@pytest.mark.parametrize("offset,expected", [(0, True), (10, False)])
def test_process_identity_checked_on_all_platforms(offset, expected):
    import psutil

    from app.services.mining_lock_admin import _pid_matches_start_time

    created = psutil.Process().create_time()
    assert _pid_matches_start_time(os.getpid(), None, created + offset) is expected


def test_unknown_process_identity_fails_closed(monkeypatch):
    import psutil

    from app.services.mining_lock_admin import _pid_matches_start_time

    monkeypatch.setattr(psutil.Process, "create_time", Mock(side_effect=psutil.AccessDenied()))
    assert _pid_matches_start_time(os.getpid(), "2026-01-01T00:00:00+00:00", 1234) is False


def test_scheduled_mining_recovers_free_lock(tmp_path, monkeypatch):
    from app.services.lab_schedule import run_scheduled_lab

    state = _api_client(tmp_path, acquired=False).app.state
    manager = Mock()
    manager.recover_interrupted.return_value = 0
    state.mining_manager = manager
    scheduled = Mock(return_value={"status": "enqueued"})
    monkeypatch.setattr("app.services.mining_schedule.run_weekly_mining", scheduled)
    monkeypatch.setattr("app.services.guardian_schedule.run_daily_guardian", lambda _: None)
    try:
        assert run_scheduled_lab(state)["mining"] == {"status": "enqueued"}
        assert state.mining_process_lock.acquired is True
        scheduled.assert_called_once_with(state)
        manager.recover_interrupted.assert_called_once_with()
    finally:
        state.mining_process_lock.release()
