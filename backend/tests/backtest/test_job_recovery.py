"""DurableJob 落盘与重启恢复扫描测试 (契约 V1 模块 A)。

全程使用 tmp_path, 不触碰项目 data/。覆盖:
- job_store 原子读写 round-trip、job_id 白名单、1 MiB 上限
- recover: 外来 lease 的 running/queued → interrupted; cancelled 不动;
  本进程 lease 不动
- optimizer/grid 实验 JSON 的 pending/running → interrupted (原始 dict 上的
  改写, 保留未知字段); 损坏文件跳过
- TTL 清理: 终态超 24h 删除、interrupted 超 7 天删除、解析失败不删
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.backtest import job_recovery
from app.backtest.job_store import (
    BOOT_ID,
    LEASE_OWNER,
    DurableJob,
    JobIdError,
    JobTooLargeError,
    BacktestJobStore,
    job_id_for_key,
    new_job,
    now_iso,
)


# ── 公共构造 ────────────────────────────────────────────────


def _foreign_running_job(job_id: str = "abc123", *, status: str = "running") -> DurableJob:
    """模拟历史进程写入的任务 (外来 lease)。"""
    now = now_iso()
    return DurableJob(
        job_id=job_id,
        job_key=f"key-{job_id}",
        kind="strategy",
        status=status,
        created_at=now,
        updated_at=now,
        heartbeat_at=now,
        lease_owner="999999:deadbeef",
        request={"strategy_id": "s", "start": "2024-01-01"},
        progress={"done": 3, "total": 10},
    )


def _experiment_payload(experiment_id: str, status: str, *, extra: dict | None = None) -> dict:
    payload = {
        "experiment_id": experiment_id,
        "status": status,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    if extra:
        payload.update(extra)
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── job_store: 原子读写与约束 ────────────────────────────────


def test_job_store_roundtrip(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    job = new_job(
        job_key="factor:0123456789ab",
        kind="factor",
        request={"factor_name": "momentum", "n_groups": 5},
    )
    store.save(job)

    assert job_id_for_key("factor:0123456789ab") == "factor-0123456789ab"
    # 纯 md5 hex 的策略 key 原样可用
    assert job_id_for_key("0123456789ab") == "0123456789ab"

    loaded = store.get(job.job_id)
    assert loaded is not None
    assert loaded.model_dump() == job.model_dump()
    assert loaded.lease_owner == LEASE_OWNER
    assert store.get_by_key("factor:0123456789ab").job_id == job.job_id

    # 覆盖写: 状态更新同一路径
    loaded.status = "completed"
    loaded.run_id = "run-1"
    store.save(loaded)
    again = store.get(job.job_id)
    assert again is not None and again.status == "completed" and again.run_id == "run-1"

    assert store.delete(job.job_id) is True
    assert store.get(job.job_id) is None
    assert store.delete(job.job_id) is False


def test_job_store_rejects_bad_job_id(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    job = _foreign_running_job("../escape")
    with pytest.raises(JobIdError):
        store.save(job)
    with pytest.raises(JobIdError):
        store.get("../escape")


def test_job_store_size_limit(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    job = _foreign_running_job()
    job.request = {"blob": "x" * (2 * 1024 * 1024)}
    with pytest.raises(JobTooLargeError):
        store.save(job)
    # 超限拒绝后不残留半文件
    assert list(store.job_dir.glob("*.json")) == []


def test_lease_owner_format() -> None:
    # lease_owner = "{pid}:{BOOT_ID}"; BOOT_ID 模块加载期间稳定
    import os

    assert LEASE_OWNER == f"{os.getpid()}:{BOOT_ID}"
    assert len(BOOT_ID) == 8


def test_save_does_not_revive_cancelled_job(tmp_path: Path) -> None:
    """磁盘已 cancelled 时, 心跳晚到的 running 写被挡住 (取消是终态)。"""
    store = BacktestJobStore(tmp_path)
    job = _foreign_running_job("cx1", status="running")
    store.save(job)
    store.save(job.model_copy(update={"status": "cancelled", "updated_at": now_iso()}))

    # 模拟心跳线程竞态: get → mutate → save (期间任务已被取消)
    stale = store.get("cx1")
    assert stale is not None
    stale.status = "running"
    stale.heartbeat_at = now_iso()
    returned = store.save(stale)

    # 返回磁盘上的 cancelled 记录, 且磁盘未被覆盖
    assert returned.status == "cancelled"
    assert store.get("cx1").status == "cancelled"


def test_save_allows_cancelled_to_cancelled_update(tmp_path: Path) -> None:
    """cancelled → cancelled 允许覆盖 (补写 error/heartbeat 等)。"""
    store = BacktestJobStore(tmp_path)
    job = _foreign_running_job("cc1", status="running")
    store.save(job)
    store.save(job.model_copy(update={"status": "cancelled", "updated_at": now_iso()}))

    store.save(job.model_copy(update={"status": "cancelled", "error": "用户取消"}))

    on_disk = store.get("cc1")
    assert on_disk is not None
    assert on_disk.status == "cancelled"
    assert on_disk.error == "用户取消"


def test_save_still_allows_running_to_interrupted(tmp_path: Path) -> None:
    """恢复扫描依赖 running→interrupted 的 save, 不能被 cancelled 守卫误伤。"""
    store = BacktestJobStore(tmp_path)
    job = _foreign_running_job("ri1", status="running")
    store.save(job)

    job.status = "interrupted"
    job.updated_at = now_iso()
    store.save(job)

    on_disk = store.get("ri1")
    assert on_disk is not None
    assert on_disk.status == "interrupted"


def test_save_skips_overwrite_when_target_unparsable(tmp_path: Path) -> None:
    """目标文件存在但解析失败时保守处理: 非 cancelled 写入不覆盖。"""
    store = BacktestJobStore(tmp_path)
    job = _foreign_running_job("br1", status="running")
    store.save(job)
    # 直接把文件写坏 (模拟旧 schema/半文件)
    (store.job_dir / "br1.json").write_text("{not json", encoding="utf-8")

    returned = store.save(job)

    assert returned is job
    # 文件原样保留, 未被覆盖
    assert (store.job_dir / "br1.json").read_text(encoding="utf-8") == "{not json"


# ── recover: backtest_jobs ──────────────────────────────────


def test_recover_marks_foreign_running_interrupted(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    store.save(_foreign_running_job("run1"))
    store.save(_foreign_running_job("queue1", status="queued"))

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    assert result["jobs_interrupted"] == 2
    for job_id in ("run1", "queue1"):
        job = store.get(job_id)
        assert job is not None
        assert job.status == "interrupted"
        # 其余字段原样保留, 供 SSE 重连整单重跑使用
        assert job.request == {"strategy_id": "s", "start": "2024-01-01"}
        assert job.progress == {"done": 3, "total": 10}


def test_recover_keeps_cancelled_and_own_lease(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    # cancelled 是用户终态, 绝不允许被改写成 interrupted
    cancelled = _foreign_running_job("cxl1", status="cancelled")
    store.save(cancelled)
    # 本进程 lease 的活跃任务 (启动扫描与新建任务并发) 不动
    own = new_job(job_key="own1", kind="strategy")
    store.save(own)

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    assert result["jobs_interrupted"] == 0
    assert store.get("cxl1").status == "cancelled"
    assert store.get("own1").status == "running"


def test_recover_skips_corrupt_job_file(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    store.save(_foreign_running_job("good1"))
    store.job_dir.mkdir(parents=True, exist_ok=True)
    (store.job_dir / "broken1.json").write_text("{not json", encoding="utf-8")

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    assert result["jobs_interrupted"] == 1
    assert result["skipped_corrupt"] >= 1
    assert store.get("good1").status == "interrupted"
    # 损坏文件原样保留, 不崩溃不删除
    assert (store.job_dir / "broken1.json").exists()


# ── recover: optimizer / grid 实验 JSON ──────────────────────


@pytest.mark.parametrize(
    "subdir",
    ["optimizer_experiments", "parameter_grid_experiments"],
)
def test_recover_marks_experiment_running_interrupted(tmp_path: Path, subdir: str) -> None:
    exp_dir = tmp_path / "research" / subdir
    _write_json(exp_dir / "so-aabbccdd.json", _experiment_payload("so-aabbccdd", "running"))
    _write_json(exp_dir / "so-11223344.json", _experiment_payload("so-11223344", "pending"))
    # 终态与未知 status 不动
    _write_json(exp_dir / "so-ffeeddcc.json", _experiment_payload("so-ffeeddcc", "cancelled"))
    _write_json(exp_dir / "so-99887766.json", _experiment_payload("so-99887766", "completed"))
    _write_json(exp_dir / "so-55667788.json", _experiment_payload("so-55667788", "weird"))
    # 损坏文件跳过
    (exp_dir / "so-badbadba.json").write_text("]]]", encoding="utf-8")

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    key = "optimizer_interrupted" if subdir == "optimizer_experiments" else "grid_interrupted"
    assert result[key] == 2
    assert result["skipped_corrupt"] == 1
    assert _read_json(exp_dir / "so-aabbccdd.json")["status"] == "interrupted"
    assert _read_json(exp_dir / "so-11223344.json")["status"] == "interrupted"
    assert _read_json(exp_dir / "so-ffeeddcc.json")["status"] == "cancelled"
    assert _read_json(exp_dir / "so-99887766.json")["status"] == "completed"
    assert _read_json(exp_dir / "so-55667788.json")["status"] == "weird"


def test_recover_experiment_preserves_unknown_fields(tmp_path: Path) -> None:
    """改写必须发生在原始 dict 上: 未来版本/其他切片新增的字段不能丢。"""
    exp_dir = tmp_path / "research" / "optimizer_experiments"
    payload = _experiment_payload(
        "so-aabbccdd",
        "running",
        extra={
            "request": {"objective": "sharpe", "universes": ["csi300"]},
            "future_field": {"nested": [1, 2, 3]},
        },
    )
    _write_json(exp_dir / "so-aabbccdd.json", payload)

    job_recovery.recover_stale_backtest_jobs(tmp_path)

    after = _read_json(exp_dir / "so-aabbccdd.json")
    assert after["status"] == "interrupted"
    assert after["request"] == {"objective": "sharpe", "universes": ["csi300"]}
    assert after["future_field"] == {"nested": [1, 2, 3]}


# ── recover: TTL 清理 ───────────────────────────────────────


def _stale_stamp(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def test_recover_deletes_expired_terminal_jobs(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    old_completed = _foreign_running_job("old1", status="completed")
    old_completed.updated_at = _stale_stamp(3)
    store.save(old_completed)
    fresh_completed = _foreign_running_job("new1", status="completed")
    store.save(fresh_completed)

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    assert result["jobs_deleted"] == 1
    assert store.get("old1") is None
    assert store.get("new1") is not None and store.get("new1").status == "completed"


def test_recover_deletes_expired_cancelled_and_keeps_fresh(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    old_cancelled = _foreign_running_job("oc1", status="cancelled")
    old_cancelled.updated_at = _stale_stamp(2)
    store.save(old_cancelled)

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    assert result["jobs_deleted"] == 1
    assert store.get("oc1") is None


def test_recover_deletes_expired_interrupted_after_seven_days(tmp_path: Path) -> None:
    store = BacktestJobStore(tmp_path)
    eight_days = _foreign_running_job("i8", status="interrupted")
    eight_days.updated_at = _stale_stamp(8)
    store.save(eight_days)
    three_days = _foreign_running_job("i3", status="interrupted")
    three_days.updated_at = _stale_stamp(3)
    store.save(three_days)

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    assert result["jobs_deleted"] == 1
    assert store.get("i8") is None
    assert store.get("i3") is not None and store.get("i3").status == "interrupted"


def test_recover_freshly_marked_interrupted_survives_ttl(tmp_path: Path) -> None:
    """刚被标为 interrupted 的任务 updated_at 已刷新, 不会立刻被 7 天规则删除。"""
    store = BacktestJobStore(tmp_path)
    stale_running = _foreign_running_job("sr1", status="running")
    stale_running.updated_at = _stale_stamp(30)
    store.save(stale_running)

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    assert result["jobs_interrupted"] == 1
    assert result["jobs_deleted"] == 0
    assert store.get("sr1") is not None and store.get("sr1").status == "interrupted"


def test_recover_keeps_job_with_unparsable_updated_at(tmp_path: Path) -> None:
    """updated_at 解析失败时保守处理: 不删除。"""
    store = BacktestJobStore(tmp_path)
    weird = _foreign_running_job("w1", status="completed")
    weird.updated_at = "not-a-date"
    store.save(weird)

    result = job_recovery.recover_stale_backtest_jobs(tmp_path)

    assert result["jobs_deleted"] == 0
    assert store.get("w1") is not None


def test_recover_empty_data_dir(tmp_path: Path) -> None:
    result = job_recovery.recover_stale_backtest_jobs(tmp_path)
    assert result == {
        "jobs_interrupted": 0,
        "optimizer_interrupted": 0,
        "grid_interrupted": 0,
        "jobs_deleted": 0,
        "skipped_corrupt": 0,
    }
