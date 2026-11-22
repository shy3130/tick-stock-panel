import json
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_engine_jobs import drain, preparing_request
from test_engine_jobs import make_manager as make_manager
from test_engine_jobs import seed as seed
from test_engine_schedules import START, schedule
from tivelo_contracts.artifacts import SnapshotBundle
from tivelo_contracts.data import DatasetReference
from tivelo_contracts.errors import TiveloError
from tivelo_data.providers import FixedSourceResolver

from app.api.engine_jobs import router
from app.backtest.worker import run_worker_task
from app.services.engine_jobs import preparation
from app.services.engine_jobs.models import JobRequest, ResearchNode, stages
from app.services.engine_jobs.preparation_models import CaptureRequest
from app.services.engine_jobs.schedule_models import ScheduleRequest
from app.services.engine_jobs.storage import JobError


def publish(manager, source):
    provider = preparation.source_provider(manager.store, source)
    data = preparation.engine(
        manager.store, FixedSourceResolver(provider, provider.configuration_digest)
    )
    plan = data.plan_sync(source.request)
    candidate = data.sync(plan)
    data.validate(candidate)
    version = data.publish(candidate)
    return DatasetReference(dataset_id=version.contents.dataset_id, version_id=version.version_id)


def subscription(nodes, watched, key="on-publication"):
    return ScheduleRequest.model_validate(
        {
            "schedule_version": "1.2",
            "schedule_key": key,
            "template": {"nodes": nodes},
            "trigger": {"kind": "data_publication", "dataset_id": watched},
        }
    )


def capture(sources=(), pins=(), name="prices", parents=()):
    return ResearchNode(
        node_id=name, depends_on=parents, capture=CaptureRequest(datasets=sources, versions=pins)
    )


def test_api_admits_each_version_once_across_restart_and_runs_exact_snapshot(
    make_manager, seed, monkeypatch
):
    manager = make_manager()
    spec = preparing_request(manager, seed).nodes[0].prepare
    (bar,) = (s for s in spec.datasets if s.request.record_type == "bar")
    prior_bar = publish(manager, bar)
    companions = tuple(publish(manager, s) for s in spec.datasets if s != bar)
    node = ResearchNode(
        node_id="daily",
        prepare=spec.model_copy(update={"datasets": (bar,), "versions": companions}),
    )
    definition = subscription([node], bar.request.dataset_id)
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    client = TestClient(app)
    base = "/api/engine-jobs"
    schema = client.get(base + "/schedules/schema").json()
    assert "default" not in schema["properties"]["overlap_policy"]
    assert schema["allOf"][0]["then"]["properties"]["overlap_policy"]["const"] == "queue"
    preview = client.post(
        base + "/preview", json=definition.template.request("preview").model_dump(mode="json")
    )
    assert preview.status_code == 200, preview.text
    assert set(preview.json()["publication_datasets"]) == {
        ref.dataset_id for ref in (prior_bar, *companions)
    }
    template = definition.model_dump(mode="json")
    saved = client.post(base + "/schedules", json=template).json()
    identity = saved["schedule_id"]
    assert saved["publication_cursor"]["sequence"] == 5 and not saved["enabled"]
    assert (
        client.post(base + f"/schedules/{identity}/enabled", json={"enabled": True}).status_code
        == 200
    )
    assert manager.dispatch_due(START) == 0
    manager.set_schedule_enabled(identity, False)
    path = manager.store.path("inputs/" + bar.path)
    versions = []
    for _ in range(2):
        path.write_bytes(path.read_bytes() + b"\n")
        versions.append(publish(manager, bar))
    assert prior_bar != versions[0] != versions[1]
    assert publish(manager, bar) == versions[1]
    path.unlink()  # The subscriber must consume the writer's sealed facts without opening this provider.
    assert manager.dispatch_due(START) == 0
    assert manager.create_schedule(definition)["publication_cursor"] == saved["publication_cursor"]
    manager.set_schedule_enabled(identity, True)
    original_finish = manager.schedules.finish

    def lost_ack(*args, **kwargs):
        raise sqlite3.OperationalError("simulated stop after admission")

    monkeypatch.setattr(manager.schedules, "finish", lost_ack)
    with pytest.raises(sqlite3.OperationalError):
        manager.dispatch_due(START)
    assert manager.schedules.get(identity)["publication_cursor"] == saved["publication_cursor"]
    (first,) = manager.store.list()
    monkeypatch.setattr(manager.schedules, "finish", original_finish)
    assert manager.shutdown()
    restored = make_manager(worker_runner=run_worker_task)
    app.state.engine_job_manager = restored
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: restored.dispatch_due(START), range(4))) == 1
    assert restored.store.list() == [first]
    for i, version in enumerate(versions):
        if i:
            assert restored.dispatch_due(START) == 1
        deliveries = client.get(base + f"/schedules/{identity}/deliveries").json()["items"]
        (delivery,) = (
            d for d in deliveries if d["publication"]["version_id"] == version.version_id
        )
        job_id = delivery["job_id"]
        assert delivery["publication"]["sequence"] == 6 + i and delivery["status"] == "submitted"
        assert restored.schedules.get(identity)["publication_cursor"]["sequence"] == 6 + i
        plan = restored.store.plan(job_id)
        assert plan.plan_version == "1.5" and not plan.inputs[0].preparation.sources
        pins = tuple(sorted((*companions, version), key=lambda ref: ref.dataset_id))
        assert plan.request.nodes[0].prepare.versions == pins
        result = drain(restored, job_id)
        assert result["status"] == "succeeded", result
        assert all(
            n["attempts"][0]["worker"]["pid"] != os.getpid() for n in result["nodes"].values()
        )
        snapshot = (
            restored.store.path(result["nodes"]["daily.snapshot"]["artifact"]["path"])
            / "bundle.json"
        )
        assert (
            SnapshotBundle.model_validate_json(snapshot.read_bytes()).snapshot.contents.datasets
            == pins
        )
        report = json.loads(
            restored.store.path(result["nodes"]["daily.report"]["artifact"]["path"]).read_bytes()
        )
        assert report["summary"]["metrics"]["final_equity"] == "5237.58"
    assert len(restored.store.list()) == 2 and len(restored.schedules.deliveries(identity)) == 2
    assert restored.dispatch_due(START) == 0 and restored.schedules.get(identity)["enabled"]
    assert len(restored.schedules.publications(dataset_id=bar.request.dataset_id).items) == 3
    assert restored.schedules.request(identity) == definition


def test_failed_admission_retries_original_receipt_and_mixed_graph_preserves_dependencies(
    make_manager, seed
):
    manager = make_manager()
    inputs = preparing_request(manager, seed).nodes[0].prepare.datasets
    (bar,) = (s for s in inputs if s.request.record_type == "bar")
    other = next(s for s in inputs if s != bar)
    nodes = [
        capture((bar,), name="first"),
        capture(
            tuple(sorted((bar, other), key=lambda s: s.request.dataset_id)),
            name="mixed",
            parents=("first",),
        ),
        capture((bar,), name="pinned", parents=("mixed",)),
    ]
    definition = subscription(nodes, bar.request.dataset_id)
    identity = manager.create_schedule(definition)["schedule_id"]
    manager.set_schedule_enabled(identity, True)
    pin = publish(manager, bar)
    source = manager.store.path("inputs/" + other.path)
    original = source.read_bytes()
    source.unlink()
    assert manager.dispatch_due(START) == 1
    (receipt,) = manager.schedules.deliveries(identity)
    assert receipt["status"] == "failed" and not manager.schedules.get(identity)["enabled"]
    assert manager.schedules.get(identity)["publication_cursor"]["sequence"] == 0
    source.write_bytes(original)
    manager.set_schedule_enabled(identity, True)
    assert manager.dispatch_due(START) == 1
    (retried,) = manager.schedules.deliveries(identity)
    assert retried["delivery_id"] == receipt["delivery_id"]
    plan = manager.store.plan(retried["job_id"])
    assert stages(plan)["pinned.snapshot"][2] == ("mixed.snapshot",)
    assert stages(plan)["mixed.sync_00"][2] == ("first.snapshot",)
    assert not manager.preview(plan.request)["trading_day_capture_nodes"]
    assert drain(manager, retried["job_id"])["status"] == "succeeded"
    assert manager.dispatch_due(START) == 0
    assert manager.schedules.get(identity)["publication_cursor"]["sequence"] == 2
    assert [
        e.version_id for e in manager.schedules.publications(dataset_id=pin.dataset_id).items
    ] == [pin.version_id]


def test_stream_replacement_is_local_to_the_subscription(make_manager, seed):
    manager = make_manager()
    source = preparing_request(manager, seed).nodes[0].prepare.datasets[0]
    record = manager.create_schedule(subscription([capture((source,))], source.request.dataset_id))
    identity = record["schedule_id"]
    manager.set_schedule_enabled(identity, True)
    other = manager.create_schedule(schedule())
    manager.set_schedule_enabled(other["schedule_id"], True)
    assert manager.shutdown()
    lake = manager.store.path("data")
    backup = lake.with_name("retained-lake")
    lake.rename(backup)
    restored = make_manager()
    assert restored.dispatch_due(START) == 1
    state = restored.schedules.get(identity)
    assert state["last_error"]["code"] == "SNAPSHOT_NOT_FOUND" and not lake.exists()
    assert state["publication_cursor"] == record["publication_cursor"]
    publish(restored, source)
    restored.set_schedule_enabled(identity, True)
    assert restored.dispatch_due(START) == 0
    assert restored.schedules.get(identity)["last_error"]["code"] == "DATA_CONFLICT"
    shutil.rmtree(lake)
    backup.rename(lake)
    restored.set_schedule_enabled(identity, True)
    publish(restored, source)
    assert restored.dispatch_due(START) == 1
    assert len(restored.schedules.deliveries(identity)) == 1


def test_validate_subscription_graph_pins_and_historical_basis(make_manager, seed):
    manager = make_manager()
    spec = preparing_request(manager, seed).nodes[0].prepare
    a, b = spec.datasets[:2]
    template = [capture((a, b))]
    a_schedule = manager.create_schedule(subscription(template, a.request.dataset_id, "a"))
    b_schedule = manager.create_schedule(subscription(template, b.request.dataset_id, "b"))
    manager.set_schedule_enabled(a_schedule["schedule_id"], True)
    with pytest.raises(JobError, match="相互触发"):
        manager.set_schedule_enabled(b_schedule["schedule_id"], True)
    manager.set_schedule_enabled(a_schedule["schedule_id"], False)
    manager.set_schedule_enabled(b_schedule["schedule_id"], True)
    with pytest.raises(ValidationError):
        subscription(
            [ResearchNode(node_id="old", run="run", snapshot="snapshot")], a.request.dataset_id
        )
    with pytest.raises(ValidationError):
        subscription(template, "unknown")
    ref = publish(manager, a.model_copy(update={"knowledge_basis": "latest_reconstruction"}))
    with pytest.raises(ValidationError):
        capture((a,), (ref,))
    with pytest.raises(ValidationError):
        capture()
    explicit = JobRequest(idempotency_key="capture", nodes=(capture(pins=(ref,)),))
    assert manager.preview(explicit)["plan"]["plan_version"] == "1.5"
    with pytest.raises(JobError) as historical:
        manager.preview(
            JobRequest(
                idempotency_key="strict",
                nodes=(
                    ResearchNode(
                        node_id="daily",
                        prepare=spec.model_copy(update={"datasets": (), "versions": (ref,)}),
                    ),
                ),
            )
        )
    assert historical.value.code == "FUTURE_INFORMATION"
    for bad, expected in (
        (ref.model_copy(update={"dataset_id": "wrong"}), JobError),
        (ref.model_copy(update={"version_id": "0" * 64}), TiveloError),
    ):
        with pytest.raises(expected):
            manager.preview(JobRequest(idempotency_key="bad", nodes=(capture(pins=(bad,)),)))


def test_concurrent_claims_and_equal_timestamps_do_not_merge_publications(make_manager, seed):
    manager = make_manager()
    source = preparing_request(manager, seed).nodes[0].prepare.datasets[0]
    definition = subscription([capture((source,))], source.request.dataset_id)
    identity = manager.create_schedule(definition)["schedule_id"]
    manager.set_schedule_enabled(identity, True)
    first = publish(manager, source)
    path = manager.store.path("inputs/" + source.path)
    path.write_bytes(path.read_bytes() + b"\n")
    second = publish(manager, source)
    data = preparation.engine(manager.store)
    with data.store.transaction() as db:
        db.execute("UPDATE publication_events SET recorded_at='2026-01-01T00:00:00.000Z'")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: manager.schedules.claim_due(START), range(8)))
    (receipt,) = manager.schedules.deliveries(identity)
    assert {d["delivery_id"] for results in claimed for d in results} == {receipt["delivery_id"]}
    assert manager.dispatch_due(START) == 1
    assert manager.dispatch_due(START) == 0
    pending, submitted = manager.schedules.deliveries(identity)
    assert (
        pending["due_at"] == submitted["due_at"]
        and pending["delivery_id"] != submitted["delivery_id"]
    )
    assert [d["publication"]["version_id"] for d in (submitted, pending)] == [
        first.version_id,
        second.version_id,
    ]
    assert manager.schedules.get(identity)["publication_cursor"]["sequence"] == 1
    manager.cancel(submitted["job_id"])
    assert manager.dispatch_due(START) == 1
    admitted = manager.schedules.deliveries(identity, limit=1)[0]
    assert drain(manager, admitted["job_id"])["status"] == "succeeded"
    # The exported bundle remains verifiable after the upstream data directory is moved.
    manager.store.path("data").rename(manager.store.path("original-lake"))
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    client = TestClient(app)
    url = f"/api/engine-jobs/{admitted['job_id']}/snapshots/prices"
    snapshot = client.get(url)
    assert snapshot.is_success and snapshot.json()["datasets"][0]["version_id"] == second.version_id
    bundle = manager.store.path(snapshot.json()["path"])
    artifact = next((bundle / "datasets").rglob("*.parquet"))
    artifact.write_bytes(artifact.read_bytes() + b"x")
    assert client.get(url).json()["detail"]["code"] == "ARTIFACT_CORRUPT"


def test_different_fact_type_and_altered_event_never_admit_a_task(make_manager, seed):
    manager = make_manager()
    a, b = preparing_request(manager, seed).nodes[0].prepare.datasets[:2]
    identity = manager.create_schedule(subscription([capture((a,))], a.request.dataset_id))[
        "schedule_id"
    ]
    manager.set_schedule_enabled(identity, True)
    publish(
        manager,
        b.model_copy(
            update={"request": b.request.model_copy(update={"dataset_id": a.request.dataset_id})}
        ),
    )
    assert manager.dispatch_due(START) == 1
    assert manager.schedules.get(identity)["last_error"]["code"] == "DATA_CONFLICT"
    (failed,) = manager.schedules.deliveries(identity)
    assert (
        not manager.store.list()
        and manager.schedules.get(identity)["publication_cursor"]["sequence"] == 0
    )
    manager.set_schedule_enabled(identity, True)
    with manager.schedules.connection() as db:
        db.execute(
            "UPDATE deliveries SET state=json_set(state,'$.publication.version_id',?) WHERE id=?",
            ("f" * 64, failed["delivery_id"]),
        )
    assert manager.dispatch_due(START) == 1
    assert manager.schedules.get(identity)["last_error"]["code"] == "ARTIFACT_CORRUPT"
    assert not manager.store.list()
