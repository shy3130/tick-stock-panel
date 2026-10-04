"""回归测试: 卡死判定从「总时长一刀切」改为「进度停滞」+ 协作式取消 + 执行槽所有权。

背景(用户反馈): 慢带宽环境冷启动全市场拉取超过 20 分钟被误标失败,
拉取线程(僵尸)仍在写盘, UI 状态与实际不对齐; 重复点击还可能撞执行锁。
均为纯逻辑, 不触网。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import pipeline_jobs, preferences
from app.services.pipeline_jobs import JobCancelledError, JobStore


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """取消标志与执行槽是模块级单例, 每个用例前后复位, 避免相互污染。"""
    pipeline_jobs._CANCEL_FLAGS.clear()
    pipeline_jobs._run_slot_owner = None
    yield
    pipeline_jobs._CANCEL_FLAGS.clear()
    pipeline_jobs._run_slot_owner = None


def _make_running_job(store: JobStore, timeout_s: int) -> str:
    jid, _ = store.create(timeout_s=timeout_s)
    store.start(jid)
    return jid


# ── 进度停滞判定 ────────────────────────────────────────────────────────

def test_stalled_job_is_reaped(monkeypatch, tmp_path):
    """无进度上报超过阈值 → 标记失败 + 置取消标志 + 释放执行槽。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    # 启动后 5 分钟无任何进度 → 停滞 300s > 60s
    stale = _iso(_now() - timedelta(minutes=5))
    store._active_jobs[jid]["started_at"] = stale
    store._active_jobs[jid]["last_progress_at"] = stale

    assert pipeline_jobs.try_acquire_run_slot(jid) is True
    store.reap_stale()

    j = store.get(jid)
    assert j["status"] == "failed"
    assert "进度停滞" in j["error"]
    # 协作式取消: 僵尸线程通过 flag 感知(记录已被 fail 弹出, flag 仍在)
    assert pipeline_jobs.is_cancelled(jid)
    # 执行槽已按所有权释放
    assert pipeline_jobs.try_acquire_run_slot("next") is True


def test_progressing_job_is_not_reaped(monkeypatch, tmp_path):
    """慢但在推进: 总时长远超阈值, 但进度心跳新鲜 → 不得误杀(核心回归)。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    # 总时长 2 小时(远超 60s 阈值), 但 10 秒前刚上报过进度
    store._active_jobs[jid]["started_at"] = _iso(_now() - timedelta(hours=2))
    store._active_jobs[jid]["last_progress_at"] = _iso(_now() - timedelta(seconds=10))

    store.reap_stale()
    assert store.get(jid)["status"] == "running"


def test_hard_cap_terminates_endless_progress(tmp_path):
    """进度回调持续但总时长超硬上限 → 兜底终止。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    beyond = timedelta(seconds=pipeline_jobs.HARD_JOB_TIMEOUT_S + 3600)
    store._active_jobs[jid]["started_at"] = _iso(_now() - beyond)
    store._active_jobs[jid]["last_progress_at"] = _iso(_now())

    store.reap_stale()
    j = store.get(jid)
    assert j["status"] == "failed"
    assert "硬上限" in j["error"]


def test_progress_updates_heartbeat(tmp_path):
    """progress() 刷新 last_progress_at(停滞计时的基准)。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    store.progress(jid, "sync", 10, "chunk 1/10")
    assert store.get(jid)["last_progress_at"] is not None


# ── 协作式取消 ──────────────────────────────────────────────────────────

def test_progress_raises_after_cancel(tmp_path):
    """取消后, 僵尸线程下一次 progress() 回调抛 JobCancelledError 自行退出。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)

    pipeline_jobs.request_cancel(jid)
    with pytest.raises(JobCancelledError):
        store.progress(jid, "sync", 20, "chunk 2/10")


def test_progress_raises_after_record_popped(tmp_path):
    """terminate() 已把记录弹出后, flag 仍需生效(僵尸靠 flag 而非记录感知)。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    store.terminate(jid, "超时自动取消")

    # 记录已从内存弹出
    assert store.get(jid)["status"] == "failed"
    with pytest.raises(JobCancelledError):
        store.progress(jid, "sync", 20, "zombie chunk")


def test_cancelled_error_survives_chunk_isolation():
    """JobCancelledError 继承 BaseException: 同步循环的分块异常隔离不得吞掉它。"""
    def chunk_loop(cancel_at: int) -> str:
        for i in range(5):
            try:
                if i == cancel_at:
                    raise JobCancelledError("j1")
            except Exception:  # noqa: BLE001  — 分块隔离的典型写法
                continue
        return "completed"

    with pytest.raises(JobCancelledError):
        chunk_loop(2)


# ── 执行槽所有权 ────────────────────────────────────────────────────────

def test_run_slot_ownership_guard():
    """非持有者的释放一律忽略 —— 僵尸线程 finally 不得误释放新任务的槽。"""
    assert pipeline_jobs.try_acquire_run_slot("jobA") is True
    assert pipeline_jobs.try_acquire_run_slot("jobB") is False

    # 旧 job(僵尸)的 finally 释放: 槽属于 jobA, 忽略
    pipeline_jobs.release_run_slot("jobB")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is False

    # 持有者自己释放后才可用
    pipeline_jobs.release_run_slot("jobA")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is True
    pipeline_jobs.release_run_slot("jobC")


def test_run_slot_reap_release_prevents_zombie_release():
    """reap 强制释放后, 僵尸晚到的同 owner 释放是幂等 no-op, 不影响新持有者。"""
    assert pipeline_jobs.try_acquire_run_slot("jobA") is True
    pipeline_jobs.release_run_slot("jobA")  # terminate 的强制释放

    assert pipeline_jobs.try_acquire_run_slot("jobB") is True  # 新任务立即入槽
    pipeline_jobs.release_run_slot("jobA")  # 僵尸 finally: owner 不匹配 → 忽略
    assert pipeline_jobs.try_acquire_run_slot("jobC") is False  # jobB 仍持有

    pipeline_jobs.release_run_slot("jobB")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is True
    pipeline_jobs.release_run_slot("jobC")


# ── 手动取消 API 端点契约 (数据页「停止」按钮) ──────────────────────────

def test_manual_cancel_endpoint_contract(monkeypatch, tmp_path):
    """POST /api/pipeline/jobs/{id}/cancel: running/pending 可停, 终态 400, 未知 404。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.pipeline import router

    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr("app.api.pipeline.job_store", store)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # 未知 job → 404
    assert client.post("/api/pipeline/jobs/nope/cancel").status_code == 404

    # running → 协作式终止: 标 failed + 置取消标志 + 释放执行槽
    jid = _make_running_job(store, timeout_s=60)
    pipeline_jobs.try_acquire_run_slot(jid)
    resp = client.post(f"/api/pipeline/jobs/{jid}/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": jid}
    j = store.get(jid)
    assert j["status"] == "failed"
    assert "手动取消" in j["error"]
    assert pipeline_jobs.is_cancelled(jid)
    assert pipeline_jobs.try_acquire_run_slot("next") is True

    # 已终态 (failed) → 400 拒绝重复取消
    assert client.post(f"/api/pipeline/jobs/{jid}/cancel").status_code == 400

    # 停止后可再建新任务 (再次拉取走完整管道的单飞基础)
    jid2, is_new = store.create(timeout_s=60)
    assert is_new is True
    assert store.active_id() == jid2


# ── 排队等待有上界(「分钟K同步卡死空转」修复) ──────────────────────────

def test_queued_pending_job_is_reaped(monkeypatch, tmp_path):
    """pending(排队等重任务槽)停滞超阈值 → 同样被回收: 可读错误 + 置取消标志。

    旧实现只回收 running: 槽位被僵尸线程/长回测占住时, 排队任务在
    「等待其他计算任务完成…」上无限空转, 且轮询触发的 reap 对它视而不见。
    """
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid, _ = store.create(timeout_s=60)  # 不 start(): 模拟排队等槽中
    store._active_jobs[jid]["last_progress_at"] = _iso(_now() - timedelta(minutes=5))

    store.reap_stale()

    j = store.get(jid)
    assert j["status"] == "failed"
    assert "排队等待重任务执行槽" in j["error"]
    assert pipeline_jobs.is_cancelled(jid)


def test_fresh_pending_job_is_not_reaped(monkeypatch, tmp_path):
    """刚进入排队的任务(停滞未超阈值)不得误杀 — create() 锚定的心跳基准生效。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid, _ = store.create(timeout_s=600)

    store.reap_stale()
    assert store.get(jid)["status"] == "pending"


def test_pending_skips_hard_total_cap(monkeypatch, tmp_path):
    """pending 无 started_at: 总时长硬上限不适用, 只按停滞判定(不会 TypeError)。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid, _ = store.create(timeout_s=pipeline_jobs.HARD_JOB_TIMEOUT_S + 3600)
    store._active_jobs[jid]["started_at"] = None
    store._active_jobs[jid]["last_progress_at"] = _iso(_now() - timedelta(seconds=10))

    store.reap_stale()
    assert store.get(jid)["status"] == "pending"


def test_run_with_capacity_fails_readably_when_slot_hogged(monkeypatch, tmp_path):
    """独占槽被长期占住(僵尸线程/长回测) → 排队任务在自身 timeout_s 内以可读
    错误失败并抛 JobCancelledError, 而不是无限等待。"""
    from app.services import heavy_job_limiter as hjl
    from app.services.heavy_job_limiter import HeavyJobLimiter

    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)

    hog = HeavyJobLimiter(capacity=2)
    monkeypatch.setattr(hjl, "shared_heavy_job_limiter", hog)
    assert hog.acquire("exclusive") is True  # 模拟占住全部预算的持有者

    jid, _ = store.create(timeout_s=1)
    ran: list[int] = []
    with pytest.raises(JobCancelledError):
        pipeline_jobs.run_with_capacity(jid, lambda: ran.append(1))

    assert ran == []  # fn 从未执行, 无数据写入
    j = store.get(jid)
    assert j["status"] == "failed"
    assert "排队等待重任务执行槽超时" in j["error"]


def test_run_with_capacity_runs_when_slot_free(monkeypatch, tmp_path):
    """槽位空闲时行为不变: 排队即时通过, fn 结果正常返回并 start。"""
    from app.services import heavy_job_limiter as hjl
    from app.services.heavy_job_limiter import HeavyJobLimiter

    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(hjl, "shared_heavy_job_limiter", HeavyJobLimiter(capacity=2))

    jid, _ = store.create(timeout_s=60)
    assert pipeline_jobs.run_with_capacity(jid, lambda: "ok") == "ok"
    # succeed() 由调用方(API task)负责; run_with_capacity 只负责 start
    assert store.get(jid)["status"] == "running"
