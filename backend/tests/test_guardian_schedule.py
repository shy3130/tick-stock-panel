"""守护每日调度与运行管理器参数化测试。"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.guardian_schedule import (
    build_guardian_fingerprint,
    guardian_request,
    run_daily_guardian,
)
from app.services.mining_jobs import MiningRunStore, MiningRunValidationError
from app.services.mining_manager import MiningJobManager


class _Repo:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    @staticmethod
    def latest_enriched_date(asset_type="stock"):
        return date(2026, 10, 23)


class _RecordingManager:
    def __init__(self, data_dir: Path) -> None:
        self.store = MiningRunStore(data_dir, namespace="guardian")
        self.starts: list[dict[str, Any]] = []

    def start(self, request, fingerprint, force=False, source="manual", run_id=None):
        self.starts.append(
            {"request": request, "fingerprint": fingerprint, "force": force,
             "source": source, "run_id": run_id}
        )
        return self.store.create(request, fingerprint, run_id=run_id)


def _app_state(data_dir: Path, manager=None):
    return SimpleNamespace(
        repo=_Repo(data_dir),
        guardian_manager=manager or _RecordingManager(data_dir),
    )


def _friday() -> datetime:
    # 2026-10-23 是周五
    return datetime(2026, 10, 23, 18, 0)


def test_disabled_returns_disabled(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": False, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4},
    )
    assert run_daily_guardian(_app_state(tmp_path), now=_friday()) == {
        "status": "disabled"
    }


def test_weekend_mismatch(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": True, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4},
    )
    saturday = datetime(2026, 10, 24, 18, 0)
    assert run_daily_guardian(_app_state(tmp_path), now=saturday) == {
        "status": "weekday_mismatch"
    }


def test_enqueues_once_then_already_claimed(tmp_path, monkeypatch):
    from app.services import preferences

    config = {"guardian_schedule_enabled": True, "guardian_watch_pct": 0.3,
              "guardian_retire_pct": 0.6, "guardian_k_max": 3}
    monkeypatch.setattr(preferences, "get_guardian_schedule", lambda: config)
    state = _app_state(tmp_path)
    manager = state.guardian_manager

    first = run_daily_guardian(state, now=_friday())
    assert first["status"] == "enqueued"
    assert first["run_id"] == "guardian-daily-2026-10-23"
    assert len(manager.starts) == 1
    started = manager.starts[0]
    assert started["source"] == "scheduled"
    assert started["run_id"] == "guardian-daily-2026-10-23"
    assert started["request"]["thresholds"] == {
        "watch_pct": 0.3, "retire_pct": 0.6, "k_max": 3,
    }
    assert started["fingerprint"]["source_claim"] == "guardian-daily-2026-10-23"
    assert started["fingerprint"]["latest_enriched_date"] == "2026-10-23"

    second = run_daily_guardian(state, now=_friday())
    assert second["status"] == "already_claimed"
    assert len(manager.starts) == 1


def test_missing_manager_raises(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": True, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4},
    )
    state = SimpleNamespace(repo=_Repo(tmp_path), guardian_manager=None)
    with pytest.raises(RuntimeError, match="guardian"):
        run_daily_guardian(state, now=_friday())


def test_fingerprint_stable_and_sensitive(tmp_path):
    repo = _Repo(tmp_path)
    first = build_guardian_fingerprint(repo)
    second = build_guardian_fingerprint(repo)
    assert first == second
    assert first["profiles"] == {"count": 0, "digest": first["profiles"]["digest"]}

    (tmp_path / "user_data" / "strategy_guardian").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user_data" / "strategy_guardian" / "mined_factor_a.json").write_text(
        "{}", encoding="utf-8"
    )
    third = build_guardian_fingerprint(repo)
    assert third["profiles"]["count"] == 1
    assert third["profiles"]["digest"] != first["profiles"]["digest"]


def test_guardian_request_shape():
    request = guardian_request({
        "guardian_watch_pct": 0.2, "guardian_retire_pct": 0.5, "guardian_k_max": 4,
    })
    assert request == {
        "strategy_ids": None,
        "thresholds": {"watch_pct": 0.2, "retire_pct": 0.5, "k_max": 4},
    }


# ── run store namespace / manager 参数化 ───────────────────────────


def test_run_store_namespace(tmp_path):
    store = MiningRunStore(tmp_path, namespace="guardian")
    assert store.runs_root == (tmp_path / "research" / "guardian" / "runs").resolve()
    assert MiningRunStore(tmp_path).runs_root == (
        tmp_path / "research" / "mining" / "runs"
    ).resolve()
    with pytest.raises(MiningRunValidationError):
        MiningRunStore(tmp_path, namespace="../escape")


def test_manager_kind_dispatches_guardian_worker(tmp_path):
    """manager kind=guardian: 线程名/worker 任务 kind/存储 namespace 全部生效。"""
    seen: dict[str, Any] = {}

    def fake_runner(task, progress_cb, cancel_event):
        seen.update(task)
        progress_cb({"type": "guardian_progress", "done": 1, "total": 1})
        return {"status": "succeeded", "checked": 1, "skipped": 0, "errors": 0,
                "results": []}

    manager = MiningJobManager(
        tmp_path,
        worker_runner=fake_runner,
        kind="guardian",
        store=MiningRunStore(tmp_path, namespace="guardian"),
    )
    manifest = manager.start({"strategy_ids": None}, {"version": "t"}, force=True,
                             source="manual")
    run_id = manifest["run_id"]

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        current = manager.store.get(run_id)
        if current and current["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert manager.store.get(run_id)["status"] == "succeeded"
    assert seen["kind"] == "guardian"
    assert seen["config"]["request"] == {"strategy_ids": None}
    assert seen["config"]["run_id"] == run_id
    assert (tmp_path / "research" / "guardian" / "runs").exists()
    manager.shutdown()


def test_manager_thread_named_by_kind(tmp_path):
    names: list[str] = []
    started = threading.Event()

    def fake_runner(task, progress_cb, cancel_event):
        names.append(threading.current_thread().name)
        started.set()
        return {"status": "succeeded", "checked": 0, "skipped": 0, "errors": 0,
                "results": []}

    manager = MiningJobManager(
        tmp_path,
        worker_runner=fake_runner,
        kind="guardian",
        store=MiningRunStore(tmp_path, namespace="guardian"),
    )
    manager.start({"strategy_ids": None}, {"version": "t"}, force=True)
    assert started.wait(timeout=2.0)
    assert names and names[0].startswith("guardian-")
    manager.shutdown()


def test_run_scheduled_lab_triggers_both(monkeypatch, tmp_path):
    """P3 单入口: 依次触发周度挖掘与每日守护, 各自异常互不阻断。"""
    import app.services.lab_schedule as lab_schedule_module

    calls: list[str] = []

    def fake_mining(state):
        calls.append("mining")
        return {"status": "enqueued"}

    def fake_guardian(state):
        calls.append("guardian")
        return {"status": "enqueued"}

    def boom(state):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "app.services.mining_schedule.run_weekly_mining", fake_mining
    )
    monkeypatch.setattr(
        "app.services.guardian_schedule.run_daily_guardian", fake_guardian
    )
    result = lab_schedule_module.run_scheduled_lab(SimpleNamespace())
    assert calls == ["mining", "guardian"]
    assert result == {"mining": {"status": "enqueued"},
                      "guardian": {"status": "enqueued"}}

    monkeypatch.setattr("app.services.mining_schedule.run_weekly_mining", boom)
    result = lab_schedule_module.run_scheduled_lab(SimpleNamespace())
    assert result["mining"] is None
    assert result["guardian"] == {"status": "enqueued"}
    del tmp_path
