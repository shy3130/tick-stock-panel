"""JobStore 可靠性契约测试 (a6a4bcd 移植)。

全程 tmp_path, 不碰项目 data/, 不依赖网络。覆盖:
- create() pending/running 单飞, 返回 (job_id, is_new)
- 手动取消 → cancelled 终态 (pending 直落 + running 协作)
- 取消记录落盘后 progress() 抛 JobCancelledError (BaseException 子类,
  能穿透管道内 except Exception 分块)
- progress 心跳维护 last_progress_at; stage 变化时 stage_pct 重置
- 慢但有持续心跳的任务不因总时长被回收
- 停滞阈值: 普通 20min / long_running 30min → failed
- 12h 总时长硬上限 → failed (有心跳也回收)
- owner guard: reap/换主后旧线程的 succeed/fail/release 全部失效
- kind 与 succeeded/degraded 旧契约保持
"""

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.api import pipeline as pipeline_api
import pytest

from app.services.pipeline_jobs import (
    JobCancelledError,
    JobStore,
    LONG_RUNNING_STALL_TIMEOUT_S,
    MAX_TOTAL_RUNTIME_S,
    STALL_TIMEOUT_S,
    _now_iso,
    _parse_ts,
)

_NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_store_creates_nested_persistence_directory(tmp_path):
    store = JobStore(store_dir=tmp_path / "nested" / "jobs")
    job_id, _ = store.create()
    owner = store.start(job_id)
    store.fail(job_id, "boom", owner=owner)
    assert (tmp_path / "nested" / "jobs" / f"{job_id}.json").exists()


def test_create_single_flight_covers_pending_and_running(tmp_path):
    store = JobStore(store_dir=tmp_path)

    # pending 窗口单飞
    j1, is_new1 = store.create(kind="daily_pipeline")
    assert is_new1 is True
    j2, is_new2 = store.create(kind="daily_pipeline")
    assert is_new2 is False
    assert j2 == j1

    # running 窗口单飞
    owner = store.start(j1)
    assert owner is not None
    j3, is_new3 = store.create()
    assert is_new3 is False
    assert j3 == j1

    # 终态后可新建
    store.progress(j1, "s", 10, "msg")
    store.succeed(j1, {"ok": True}, owner=owner)
    j4, is_new4 = store.create()
    assert is_new4 is True
    assert j4 != j1


def test_start_cannot_reassign_running_execution_slot(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    owner = store.start(job_id)
    assert owner is not None
    assert store.start(job_id) is None
    assert store.execution_owner() == job_id
    store.fail(job_id, "done", owner=owner)


def test_cancel_pending_job_lands_cancelled_terminal(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(kind="daily_pipeline")

    assert store.cancel(job_id) is True

    job = store.get(job_id)
    assert job["status"] == "cancelled"
    assert job["error"] == "用户手动取消"
    assert store.active_id() is None
    # 已终态不可重复取消
    assert store.cancel(job_id) is False


def test_cancel_api_persists_cancelled_terminal(monkeypatch, tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(kind="daily_pipeline")
    store.start(job_id)
    monkeypatch.setattr(pipeline_api, "job_store", store)

    assert pipeline_api.cancel_job(job_id) == {"cancelled": job_id}
    assert store.get(job_id)["status"] == "cancelled"
    with pytest.raises(HTTPException) as exc_info:
        pipeline_api.cancel_job(job_id)
    assert exc_info.value.status_code == 400


def test_cancel_running_job_cooperative_then_persisted(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    owner = store.start(job_id)
    store.progress(job_id, "sync_daily", 10, "同步中")

    store.cancel(job_id)

    # cancelled 落盘为终态, 与 failed 区分
    job = store.get(job_id)
    assert job["status"] == "cancelled"
    # 执行中的 worker 下一次 progress 感知取消
    with pytest.raises(JobCancelledError):
        store.progress(job_id, "sync_daily", 50, "不应到达")
    # 旧线程的终态写被 owner guard 挡住, cancelled 不被覆盖
    store.succeed(job_id, {"universe_size": 1}, owner=owner)
    assert store.get(job_id)["status"] == "cancelled"


def test_job_cancelled_error_is_baseexception_not_exception(tmp_path):
    """取消信号必须穿透管道内 `except Exception` 分块。"""
    assert issubclass(JobCancelledError, BaseException)
    assert not issubclass(JobCancelledError, Exception)

    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    owner = store.start(job_id)
    store.cancel(job_id)

    def pipeline_stage():
        try:
            store.progress(job_id, "sync_daily", 20, "阶段内")
        except Exception:
            raise AssertionError("取消信号被 except Exception 吞掉") from None

    with pytest.raises(JobCancelledError):
        pipeline_stage()


def test_cancelled_record_reappearing_progress_still_senses_cancel(tmp_path):
    """记录已被弹出内存(落盘)后, 迟到的 progress 仍感知取消而非静默成功。"""
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    store.start(job_id)
    store.cancel(job_id)
    assert job_id not in store._active_jobs

    with pytest.raises(JobCancelledError):
        store.progress(job_id, "sync_daily", 90, "迟到的心跳")


def test_progress_updates_heartbeat_and_stage_pct_reset(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    assert store.start(job_id) is not None
    store._active_jobs[job_id]["last_progress_at"] = _now_iso_from(
        datetime.now(timezone.utc) - timedelta(seconds=5)
    )
    started = _parse_ts(store.get(job_id)["last_progress_at"])

    store.progress(job_id, "sync_daily", 10, "批次 1/10", stage_pct=10, skip_log=True)
    job = store.get(job_id)
    assert job["stage_pct"] == 10
    assert _parse_ts(job["last_progress_at"]) > started

    # stage 切换且未显式给 stage_pct → 重置为 0 (修复: 先赋 stage 再比较恒 False 的旧 bug)
    store.progress(job_id, "compute_enriched", 60, "新阶段")
    assert store.get(job_id)["stage_pct"] == 0
    # 同 stage 未给 stage_pct → 保留现值
    store.progress(job_id, "compute_enriched", 70, "继续")
    assert store.get(job_id)["stage_pct"] == 0
    # 显式 stage_pct 优先
    store.progress(job_id, "compute_enriched", 80, "批次", stage_pct=42)
    assert store.get(job_id)["stage_pct"] == 42


def _backdate(store: JobStore, job_id: str, *, started_ago_s: float, progress_ago_s: float) -> None:
    job = store._active_jobs[job_id]
    job["started_at"] = _now_iso_from(_NOW - timedelta(seconds=started_ago_s))
    job["last_progress_at"] = _now_iso_from(_NOW - timedelta(seconds=progress_ago_s))


def _now_iso_from(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_reap_stalled_normal_job_after_20min(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    store.start(job_id)
    _backdate(
        store, job_id, started_ago_s=STALL_TIMEOUT_S + 60, progress_ago_s=STALL_TIMEOUT_S + 60
    )

    reaped = store.reap_stale(now=_NOW)

    assert len(reaped) == 1
    job = store.get(job_id)
    assert job["status"] == "failed"
    assert "无进度" in job["error"]
    assert store.active_id() is None


def test_reap_stalled_long_running_uses_30min_threshold(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(long_running=True)
    store.start(job_id)
    # 普通 20min 阈值已过但未到 30min → 不回收
    _backdate(
        store, job_id, started_ago_s=STALL_TIMEOUT_S + 120, progress_ago_s=STALL_TIMEOUT_S + 120
    )
    assert store.reap_stale(now=_NOW) == []
    # 超过 30min → 回收
    _backdate(
        store,
        job_id,
        started_ago_s=LONG_RUNNING_STALL_TIMEOUT_S + 60,
        progress_ago_s=LONG_RUNNING_STALL_TIMEOUT_S + 60,
    )
    assert len(store.reap_stale(now=_NOW)) == 1
    assert store.get(job_id)["status"] == "failed"


def test_slow_job_with_heartbeat_not_reaped_by_stall(tmp_path):
    """持续有心跳的慢任务绝不因停滞被回收 — 心跳重置停滞窗口。"""
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    store.start(job_id)
    # 运行远超 20min, 但最后一次心跳就在 1 分钟前
    _backdate(store, job_id, started_ago_s=STALL_TIMEOUT_S * 10, progress_ago_s=60)

    assert store.reap_stale(now=_NOW) == []
    assert store.get(job_id)["status"] == "running"


def test_reap_12h_hard_cap_even_with_heartbeat(tmp_path):
    """总时长 12h 硬上限: 有心跳也必须回收。"""
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(long_running=True)
    store.start(job_id)
    _backdate(store, job_id, started_ago_s=MAX_TOTAL_RUNTIME_S + 60, progress_ago_s=30)

    reaped = store.reap_stale(now=_NOW)

    assert len(reaped) == 1
    job = store.get(job_id)
    assert job["status"] == "failed"
    assert "12小时" in job["error"]


def test_reap_pending_orphan_job(tmp_path):
    """create 后从未 start 的孤儿 job (如 reload 丢失协程) 超阈值回收。"""
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    store._active_jobs[job_id]["created_at"] = _now_iso_from(
        _NOW - timedelta(seconds=STALL_TIMEOUT_S + 60)
    )

    assert len(store.reap_stale(now=_NOW)) == 1
    assert store.get(job_id)["status"] == "failed"


def test_owner_guard_reaped_worker_cannot_overwrite_terminal(tmp_path):
    """reap 后旧线程的 succeed/fail 不得复活或覆盖终态。"""
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    owner = store.start(job_id)
    _backdate(
        store, job_id, started_ago_s=STALL_TIMEOUT_S + 60, progress_ago_s=STALL_TIMEOUT_S + 60
    )
    store.reap_stale(now=_NOW)
    assert store.get(job_id)["status"] == "failed"

    # 旧线程拿着失效 owner 试图写终态 → 全部忽略
    store.succeed(job_id, {"universe_size": 5}, owner=owner)
    store.fail(job_id, "late error", owner=owner)
    store.release(job_id, owner)
    assert store.get(job_id)["status"] == "failed"
    assert store.get(job_id)["result"] is None


def test_owner_guard_wrong_owner_cannot_release_new_owners_slot(tmp_path):
    """旧 token 不能释放新任务的执行槽。"""
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    owner1 = store.start(job_id)

    # reap 走 _finish_locked 清掉槽位; 新一轮 create/start 换新 owner
    _backdate(
        store, job_id, started_ago_s=STALL_TIMEOUT_S + 60, progress_ago_s=STALL_TIMEOUT_S + 60
    )
    store.reap_stale(now=_NOW)
    job_id2, _ = store.create()
    owner2 = store.start(job_id2)
    assert owner2 != owner1

    # 旧线程 finally: 用旧 token 释放 → 不得影响新 job 的全局执行槽
    store.release(job_id2, owner1)
    store.release(job_id, owner1)
    assert store.execution_owner() == job_id2
    # 新 owner 仍持有执行槽并正常收敛终态
    store.progress(job_id2, "s", 10, "m")
    store.succeed(job_id2, {"minute_rows": 1}, owner=owner2)
    assert store.execution_owner() is None
    assert store.get(job_id2)["status"] == "succeeded"


def test_start_returns_none_after_cancel(tmp_path):
    """create 与 start 之间被取消 → start 返回 None, worker 必须放弃执行。"""
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    store.cancel(job_id)
    assert store.start(job_id) is None


def test_job_with_failed_stages_finishes_degraded(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create()
    owner = store.start(job_id)

    result = {
        "minute_rows": 0,
        "failed_stages": [{"stage": "sync_minute", "error": "catalog stale"}],
    }
    store.succeed(job_id, result, owner=owner)

    job = store.get(job_id)
    assert job is not None
    assert job["status"] == "degraded"
    assert job["result"] == result
    assert job["error"] is None
    assert store.active_id() is None


def test_job_kind_is_persisted_in_terminal_summary(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(kind="daily_pipeline")
    owner = store.start(job_id)
    store.fail(job_id, "failed before first progress event", owner=owner)

    job = store.list_recent(limit=1)[0]
    assert job["kind"] == "daily_pipeline"
    assert job["stage"] == "init"


def test_cancelled_visible_in_list_recent_and_persisted(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(kind="daily_pipeline")
    store.start(job_id)
    store.cancel(job_id)

    summary = store.list_recent(limit=5)[0]
    assert summary["status"] == "cancelled"
    # 落盘后可从磁盘读回
    assert store.get(job_id)["status"] == "cancelled"
