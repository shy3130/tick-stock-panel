import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_engine_jobs import drain, request
from test_engine_jobs import make_manager as make_manager
from test_engine_jobs import seed as seed

from app.api.engine_jobs import router
from app.services.engine_jobs.schedule_models import IntervalTrigger, ScheduleRequest
from app.services.engine_jobs.storage import JobError

START = datetime(2026, 11, 18, 1, tzinfo=UTC)


def schedule(key="daily", **changes):
    payload = {
        "schedule_key": key,
        "trigger": {"starts_at": START.isoformat(), "interval_seconds": 60},
        "template": request().model_dump(
            mode="json", exclude={"idempotency_key", "request_version"}
        ),
        **changes,
    }
    return ScheduleRequest.model_validate(payload)


def enabled(manager, key="daily", **changes):
    saved = manager.create_schedule(schedule(key, **changes))
    assert not saved["enabled"]
    return manager.set_schedule_enabled(saved["schedule_id"], True)


def test_interval_normalization_and_boundaries():
    trigger = IntervalTrigger(starts_at="2026-11-18T09:00:00+08:00", interval_seconds=60)
    assert trigger.starts_at == START
    assert trigger.latest_due(START - timedelta(microseconds=1)) is None
    assert trigger.latest_due(START + timedelta(seconds=179)) == START + timedelta(seconds=120)
    end = IntervalTrigger(
        starts_at=START, interval_seconds=60, ends_at=START + timedelta(seconds=90)
    )
    assert end.latest_due(START + timedelta(days=10)) == START + timedelta(seconds=60)
    for changes in (
        {"starts_at": "2026-11-18T09:00:00"},
        {"interval_seconds": True},
        {"interval_seconds": 1},
        {"ends_at": START - timedelta(seconds=1)},
    ):
        with pytest.raises(ValidationError):
            IntervalTrigger.model_validate({"starts_at": START, "interval_seconds": 60, **changes})
    with pytest.raises(ValidationError):
        schedule(misfire_policy="all")
    with pytest.raises(ValidationError):
        schedule(template={**schedule().template.model_dump(), "nodes": []})


def test_paused_creation_idempotence_and_immutable_definition(make_manager):
    manager = make_manager()
    saved = manager.create_schedule(schedule())
    assert manager.create_schedule(schedule()) == saved
    assert manager.dispatch_due(START) == 0 and not manager.store.list()
    with pytest.raises(JobError, match="计划键"):
        manager.create_schedule(schedule(trigger={"starts_at": START, "interval_seconds": 120}))
    assert manager.schedules.get(saved["schedule_id"])["request"] == schedule().model_dump(
        mode="json"
    )
    with manager.store.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_missed_slots_rollback_overlap_and_restart_keep_one_job(make_manager):
    manager = make_manager()
    saved = enabled(manager)
    tick = START + timedelta(seconds=190)
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda _: manager.dispatch_due(tick), range(8)))
    assert sum(counts) == 1
    first = manager.schedules.deliveries(saved["schedule_id"])[0]
    assert first["status"] == "submitted" and first["missed_count"] == 3
    assert first["due_at"] == (START + timedelta(seconds=180)).isoformat()
    assert manager.dispatch_due(START) == 0
    assert manager.dispatch_due(START + timedelta(seconds=240)) == 1
    skipped = manager.schedules.deliveries(saved["schedule_id"])[0]
    assert skipped["status"] == "skipped_overlap" and skipped["job_id"] is None
    assert manager.dispatch_due(START + timedelta(seconds=300)) == 1
    assert len(manager.store.list()) == 1
    assert manager.schedules.deliveries(saved["schedule_id"])[0]["status"] == "skipped_overlap"
    manager.cancel(first["job_id"])
    assert manager.shutdown()
    restored = make_manager()
    assert restored.dispatch_due(START + timedelta(seconds=300)) == 0
    assert restored.dispatch_due(START + timedelta(seconds=360)) == 1
    latest = restored.schedules.deliveries(saved["schedule_id"])[0]
    assert latest["status"] == "submitted" and latest["job_id"] != first["job_id"]
    finished = drain(restored, latest["job_id"])
    assert finished["status"] == "succeeded"
    report = json.loads(
        restored.store.path(finished["nodes"]["first.report"]["artifact"]["path"]).read_text()
    )
    assert report["summary"]["metrics"]["final_equity"] == "5237.58"


def test_prepared_dispatch_survives_pause_restart_and_input_change(make_manager, monkeypatch):
    manager = make_manager()
    saved = enabled(manager)
    original = manager._submit_plan
    monkeypatch.setattr(
        manager, "_submit_plan", lambda plan: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        manager.dispatch_due(START)
    delivery = manager.schedules.deliveries(saved["schedule_id"])[0]
    assert delivery["status"] == "prepared" and delivery["plan_id"]
    manager.set_schedule_enabled(saved["schedule_id"], False)
    monkeypatch.setattr(manager, "_submit_plan", original)
    assert manager.dispatch_due(START) == 0 and not manager.store.list()
    manager.store.path("inputs/run/run-lock.json").write_text("{}")
    assert manager.shutdown()
    restored = make_manager()
    restored.set_schedule_enabled(saved["schedule_id"], True)
    assert restored.dispatch_due(START) == 1
    resumed = restored.schedules.deliveries(saved["schedule_id"])[0]
    assert resumed["plan_id"] == delivery["plan_id"]
    assert resumed["job_id"] == delivery["plan_id"]
    assert drain(restored, resumed["job_id"])["status"] == "failed"


def test_submitted_job_is_reconciled_without_reopening_changed_inputs(make_manager, monkeypatch):
    manager = make_manager()
    saved = enabled(manager)
    original = manager.schedules.finish
    monkeypatch.setattr(
        manager.schedules,
        "finish",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("receipt unavailable")
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        manager.dispatch_due(START)
    job = manager.store.list()[0]
    manager.store.path("inputs/run/run-lock.json").write_text("{}")
    monkeypatch.setattr(manager.schedules, "finish", original)
    assert manager.shutdown()
    restored = make_manager()
    assert restored.dispatch_due(START) == 1
    assert len(restored.store.list()) == 1
    delivery = restored.schedules.deliveries(saved["schedule_id"])[0]
    assert delivery["status"] == "submitted" and delivery["job_id"] == job["job_id"]


def test_invalid_input_pauses_only_its_schedule_without_old_snapshot_fallback(make_manager):
    manager = make_manager()
    bad = schedule("bad").model_dump(mode="json")
    bad["template"]["nodes"][0]["snapshot"] = "missing"
    record = manager.create_schedule(ScheduleRequest.model_validate(bad))
    manager.set_schedule_enabled(record["schedule_id"], True)
    good = enabled(manager, "good")
    assert manager.dispatch_due(START) == 2
    failed = manager.schedules.deliveries(record["schedule_id"])[0]
    assert failed["status"] == "failed" and failed["job_id"] is None
    assert not manager.schedules.get(record["schedule_id"])["enabled"]
    assert manager.schedules.get(good["schedule_id"])["enabled"]
    assert len(manager.store.list()) == 1
    assert manager.dispatch_due(START) == 0


def test_end_boundary_and_bounded_lists(make_manager):
    manager = make_manager()
    saved = enabled(manager, trigger={"starts_at": START, "interval_seconds": 60, "ends_at": START})
    assert manager.dispatch_due(START) == 1
    assert manager.schedules.get(saved["schedule_id"])["next_due_at"] is None
    assert not manager.schedules.get(saved["schedule_id"])["enabled"]
    with pytest.raises(JobError, match="时点已全部处理"):
        manager.set_schedule_enabled(saved["schedule_id"], True)
    assert manager.dispatch_due(START + timedelta(days=100)) == 0
    for limit in (0, 101, True):
        with pytest.raises(ValueError):
            manager.schedules.deliveries(saved["schedule_id"], limit=limit)


def test_unknown_schedule_schema_disables_only_schedules(make_manager):
    manager = make_manager()
    database = manager.schedules.database
    assert manager.shutdown()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=2")
    restored = make_manager()
    assert restored.schedules is None
    assert restored.submit(request())["status"] == "queued"
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = restored
    with TestClient(app) as client:
        assert client.get("/api/engine-jobs/schedules").status_code == 503
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_schedule_api_and_paused_creation(make_manager):
    manager = make_manager()
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    with TestClient(app) as client:
        assert "template" in client.get("/api/engine-jobs/schedules/schema").json()["properties"]
        data = schedule().model_dump(mode="json")
        saved = client.post("/api/engine-jobs/schedules", json=data).json()
        identity = saved["schedule_id"]
        assert not saved["enabled"] and not manager.store.list()
        assert client.get("/api/engine-jobs/schedules").json()["items"][0] == saved
        assert client.post(
            f"/api/engine-jobs/schedules/{identity}/enabled", json={"enabled": True}
        ).json()["enabled"]
        manager.dispatch_due(START)
        delivery = client.get(f"/api/engine-jobs/schedules/{identity}/deliveries").json()["items"][
            0
        ]
        assert client.get(f"/api/engine-jobs/{delivery['job_id']}").json()["status"] == "queued"
        assert client.get("/api/engine-jobs/schedules/missing").status_code == 404
        assert (
            client.get(f"/api/engine-jobs/schedules/{identity}/deliveries?limit=101").status_code
            == 422
        )
        assert (
            client.post(
                f"/api/engine-jobs/schedules/{identity}/enabled", json={"enabled": "yes"}
            ).status_code
            == 422
        )


def test_background_trigger_runs_while_worker_is_busy_and_shutdown_holds_owner(
    make_manager, monkeypatch
):
    entered, release = threading.Event(), threading.Event()

    def held_worker(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        raise JobError("CANCELLED", "test stopped")

    manager = make_manager(worker_runner=held_worker, autostart=True)
    submitted = threading.Event()
    original = manager.schedules.finish

    def receipt(*args, **kwargs):
        result = original(*args, **kwargs)
        if result["status"] == "submitted":
            submitted.set()
        return result

    monkeypatch.setattr(manager.schedules, "finish", receipt)
    try:
        first = manager.submit(request())
        assert entered.wait(5)
        tick = datetime.now(UTC).replace(microsecond=0)
        enabled(manager, trigger={"starts_at": tick, "interval_seconds": 60})
        assert submitted.wait(5)
        assert len(manager.store.list()) == 2
        assert not manager.shutdown(timeout=0.01)
        assert manager._owner.acquired
    finally:
        release.set()
        assert manager.shutdown()
    assert manager.store.get(first["job_id"])["status"] == "interrupted"


def test_unprepared_request_key_collision_is_not_adopted(make_manager):
    manager = make_manager()
    saved = enabled(manager)
    delivery = manager.schedules.claim_due(START)[0]
    unrelated = manager.submit(schedule().template.request(delivery["request_key"]))
    assert manager.dispatch_due(START) == 1
    failed = manager.schedules.deliveries(saved["schedule_id"])[0]
    assert failed["status"] == "failed" and failed["error"]["code"] == "DATA_CONFLICT"
    assert failed["job_id"] is None
    assert manager.store.get(unrelated["job_id"])["status"] == "queued"


def test_queue_admission_failure_retains_locked_plan_and_pauses(make_manager, monkeypatch):
    manager = make_manager()
    saved = enabled(manager)

    def full(plan):
        raise JobError("RESOURCE_LIMIT", "test queue full")

    monkeypatch.setattr(manager.store, "create", full)
    assert manager.dispatch_due(START) == 1
    failed = manager.schedules.deliveries(saved["schedule_id"])[0]
    assert failed["status"] == "failed" and failed["error"]["code"] == "RESOURCE_LIMIT"
    assert manager.schedules.plan(failed).plan_id == failed["plan_id"]
    assert not manager.schedules.get(saved["schedule_id"])["enabled"]
    assert not manager.store.list()


def test_pending_outbox_and_reference_paths_are_preserved(make_manager):
    manager = make_manager()
    saved = enabled(manager)
    first = manager.schedules.claim_due(START)[0]
    assert first["status"] == "pending" and first["plan_id"] is None
    assert manager.shutdown()
    restored = make_manager()
    assert restored.dispatch_due(START + timedelta(seconds=90)) == 1
    delivery = restored.schedules.deliveries(saved["schedule_id"])[0]
    assert delivery["delivery_id"] == first["delivery_id"]
    assert restored.store.plan(delivery["job_id"]).request.nodes == schedule().template.nodes
    assert restored.dispatch_due(START + timedelta(seconds=90)) == 1
    assert restored.schedules.deliveries(saved["schedule_id"])[0]["status"] == "skipped_overlap"


def test_schedule_file_symlink_and_limits_fail_closed(tmp_path):
    from app.services.engine_jobs.schedules import ScheduleStore
    from app.services.engine_jobs.storage import JobStore

    jobs = JobStore(tmp_path)
    external = tmp_path / "external.sqlite3"
    external.write_bytes(b"untouched")
    (jobs.root / "schedules.sqlite3").symlink_to(external)
    with pytest.raises(JobError, match="符号链接"):
        ScheduleStore(jobs)
    assert external.read_bytes() == b"untouched"


def test_schedule_count_and_enabled_capacity_are_bounded(make_manager):
    manager = make_manager()
    ids = [manager.create_schedule(schedule(f"bounded.{i}"))["schedule_id"] for i in range(256)]
    assert len(manager.schedules.list(limit=100)) == 100
    with pytest.raises(JobError, match="数量"):
        manager.create_schedule(schedule("one-too-many"))
    for identity in ids[:64]:
        manager.set_schedule_enabled(identity, True)
    with pytest.raises(JobError, match="同时启用"):
        manager.set_schedule_enabled(ids[64], True)
    manager.set_schedule_enabled(ids[0], False)
    assert manager.set_schedule_enabled(ids[64], True)["enabled"]


def test_committed_job_wins_admission_acknowledgement_failure(make_manager, monkeypatch):
    manager = make_manager()
    saved = enabled(manager)
    original = manager._submit_plan

    def lost_acknowledgement(plan):
        original(plan)
        manager.store.path("inputs/run/run-lock.json").write_text("{}")
        raise OSError("commit succeeded but acknowledgement was lost")

    monkeypatch.setattr(manager, "_submit_plan", lost_acknowledgement)
    assert manager.dispatch_due(START) == 1
    delivery = manager.schedules.deliveries(saved["schedule_id"])[0]
    assert delivery["status"] == "submitted"
    assert delivery["job_id"] == manager.store.list()[0]["job_id"]
    assert manager.dispatch_due(START) == 0
    assert len(manager.store.list()) == 1


def test_shutdown_during_trigger_preserves_owner_and_prepared_dispatch(make_manager, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    manager = make_manager(autostart=True)
    original = manager._submit_plan

    def held_admission(plan):
        entered.set()
        assert release.wait(10)
        return original(plan)

    monkeypatch.setattr(manager, "_submit_plan", held_admission)
    saved = enabled(
        manager,
        trigger={"starts_at": datetime.now(UTC).replace(microsecond=0), "interval_seconds": 60},
    )
    try:
        assert entered.wait(5)
        assert not manager.shutdown(timeout=0.01)
        assert manager._owner.acquired
    finally:
        release.set()
        assert manager.shutdown()
    pending = manager.schedules.deliveries(saved["schedule_id"])[0]
    assert pending["status"] == "prepared" and not manager.store.list()
    restored = make_manager()
    assert restored.dispatch_due() == 1
    assert restored.store.list()[0]["plan_id"] == pending["plan_id"]
