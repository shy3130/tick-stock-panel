import json
import os
import shutil
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_tivelo_execution_integration import configured_market
from test_tivelo_execution_integration import (
    test_published_data_snapshot_drives_the_independent_daily_runner as build_seed,
)
from tivelo_backtest.study_models import SearchAxis, SearchSpace, StudyRequest, WalkForwardWindows

from app.api.engine_jobs import router
from app.backtest.worker import BacktestWorkerError
from app.services.engine_jobs.artifacts import execute_node
from app.services.engine_jobs.manager import EngineJobManager
from app.services.engine_jobs.models import (
    JobRequest,
    PipelineManifest,
    ResearchNode,
    ResearchReport,
)
from app.services.engine_jobs.storage import JobError, JobStore
from app.services.heavy_job_limiter import HeavyJobLimiter
from app.services.mining_process_lock import MiningProcessLock, MiningProcessLockError


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    directory = tmp_path_factory.mktemp("engine-inputs")
    build_seed(configured_market(), directory)
    return directory


class Observer:
    def __init__(self, callback, cancellation):
        self.callback, self.cancellation = callback, cancellation

    def cancelled(self):
        return self.cancellation.is_set()

    def progress(self, done, total, phase):
        self.callback({"done": done, "total": total, "phase": phase})


def inline_worker(task, callback, cancellation, **limits):
    """Real calculation without spawn, for deterministic lifecycle fault injection."""
    store = JobStore(task["data_dir"])
    plan = store.plan(task["job_id"])
    _, attempt = store.current(task["job_id"], task["node_id"], task["token"])
    directory = store.attempt_directory(plan.plan_id, task["node_id"], attempt["number"])
    directory.mkdir(parents=True, exist_ok=True)
    lock = MiningProcessLock(directory)
    lock.acquire(role="test-node")
    try:
        artifact = execute_node(
            store, plan, task["node_id"], attempt, Observer(callback, cancellation)
        )
        return {"artifact": artifact.model_dump(mode="json")}
    finally:
        lock.release()


@pytest.fixture
def make_manager(tmp_path, seed, monkeypatch):
    import app.services.engine_jobs.manager as module
    import app.services.heavy_job_limiter as limiter_module

    monkeypatch.setattr(limiter_module, "_memory_admission_ok", lambda: True)
    limiter = HeavyJobLimiter(capacity=2, queue_timeout_s=0.1)
    monkeypatch.setattr(module, "shared_heavy_job_limiter", limiter)
    store = JobStore(tmp_path)
    for source, target in (("controlled-run", "run"), ("portable", "snapshot")):
        shutil.copytree(seed / source, store.path("inputs/" + target))
    managers = []

    def create(**kwargs):
        kwargs.setdefault("autostart", False)
        kwargs.setdefault("worker_runner", inline_worker)
        manager = EngineJobManager(tmp_path, **kwargs)
        managers.append(manager)
        return manager

    yield create
    for manager in managers:
        assert manager.shutdown()
    assert limiter.in_use == 0


def request(*, key="test-1", study=False, **changes):
    nodes = [ResearchNode(node_id="first", run="run", snapshot="snapshot")]
    if study:
        nodes.append(
            ResearchNode(
                node_id="second",
                run="run",
                snapshot="snapshot",
                depends_on=("first",),
                study=StudyRequest(
                    mode="walk_forward",
                    search=SearchSpace(
                        target="controlled_program",
                        axes=(SearchAxis(name="weight_per_position", values=("0.5", "0.75")),),
                    ),
                    walk_forward=WalkForwardWindows(
                        train_sessions=2, test_sessions=1, step_sessions=1
                    ),
                ),
            )
        )
    return JobRequest(idempotency_key=key, nodes=tuple(nodes), **changes)


def drain(manager, job_id, maximum=12):
    for _ in range(maximum):
        manager.run_once()
        state = manager.store.get(job_id)
        if state["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return state
    pytest.fail(f"job did not terminate: {manager.store.get(job_id)}")


def test_real_spawn_dag_records_reports_and_reuses_after_restart(make_manager):
    from app.backtest.worker import run_worker_task

    manager = make_manager(worker_runner=run_worker_task)
    spec = request(study=True)
    created = manager.submit(spec)
    assert manager.submit(spec)["job_id"] == created["job_id"]
    finished = drain(manager, created["job_id"])
    assert finished["status"] == "succeeded", finished
    assert all(
        node["attempts"][0]["worker"]["pid"] != os.getpid() for node in finished["nodes"].values()
    )
    assert all(
        node["attempts"][0]["worker"]["peak_rss_bytes"] > 0 for node in finished["nodes"].values()
    )
    manifest = PipelineManifest.model_validate_json(
        manager.store.path(finished["artifact"]["path"]).read_bytes()
    )
    assert set(manifest.artifacts) == set(finished["nodes"])
    first = ResearchReport.model_validate_json(
        manager.store.path(manifest.artifacts["first.report"].path).read_bytes()
    )
    assert first.summary["metrics"]["final_equity"] == "5237.58"
    second = ResearchReport.model_validate_json(
        manager.store.path(manifest.artifacts["second.report"].path).read_bytes()
    )
    assert second.summary["report"]["oos_compounded_return"] == "0"
    assert second.summary["report"]["windows"][0]["test"]["metrics"]["filled_orders"] == 0
    assert manager.shutdown()
    restarted = make_manager()
    assert restarted.submit(spec) == finished
    assert restarted.run_once() is False
    assert restarted.store.plan(created["job_id"]) == manager.store.plan(created["job_id"])


def test_failed_node_blocks_dependents_and_retry_reuses_verified_parent(make_manager):
    def failing(task, *args, **kwargs):
        if task["node_id"] == "first.execute":
            raise BacktestWorkerError("private strategy text MUST NOT leak", code="STRATEGY_FAILED")
        return inline_worker(task, *args, **kwargs)

    manager = make_manager(worker_runner=failing)
    job_id = manager.submit(request(study=True))["job_id"]
    failed = drain(manager, job_id)
    assert failed["status"] == "failed"
    assert failed["nodes"]["first.execute"]["attempts"][0]["error"] == {"code": "STRATEGY_FAILED"}
    assert "private strategy" not in json.dumps(failed)
    assert failed["nodes"]["second.validate"]["attempts"] == []
    manager._worker = inline_worker
    manager.retry(job_id)
    result = drain(manager, job_id)
    assert result["status"] == "succeeded"
    assert len(result["nodes"]["first.validate"]["attempts"]) == 1
    assert [item["status"] for item in result["nodes"]["first.execute"]["attempts"]] == [
        "failed",
        "succeeded",
    ]


def test_recovery_checks_live_lock_and_fences_delayed_dispatch(make_manager):
    manager = make_manager()
    job_id = manager.submit(request())["job_id"]
    attempt = manager.store.begin(job_id, "first.validate")
    directory = manager.store.attempt_directory(job_id, "first.validate", attempt["number"])
    directory.mkdir(parents=True)
    live = MiningProcessLock(directory)
    live.acquire(role="test-live-worker")
    try:
        assert manager.recover(job_id) is False
        assert manager.store.get(job_id)["status"] == "running"
        with pytest.raises(MiningProcessLockError):
            make_manager()
    finally:
        live.release()
    assert manager.recover(job_id) is True
    assert manager.store.get(job_id)["status"] == "interrupted"
    manager.retry(job_id)
    with pytest.raises(JobError, match="失效"):
        manager.store.current(job_id, "first.validate", attempt["token"])
    assert drain(manager, job_id)["status"] == "succeeded"


def test_recovery_keeps_a_committed_result_after_lost_worker_ack(make_manager):
    manager = make_manager()
    job_id = manager.submit(request())["job_id"]
    manager.run_once()
    attempt = manager.store.begin(job_id, "first.execute")
    inline_worker(
        {
            "data_dir": manager.data_dir,
            "job_id": job_id,
            "node_id": "first.execute",
            "token": attempt["token"],
        },
        lambda message: None,
        threading.Event(),
    )
    assert manager.shutdown()
    restored = make_manager()
    assert restored.recover(job_id)
    assert restored.store.get(job_id)["nodes"]["first.execute"]["status"] == "succeeded"
    result = drain(restored, job_id)
    assert result["status"] == "succeeded"
    assert len(result["nodes"]["first.execute"]["attempts"]) == 1


def test_cancel_before_execution_and_after_final_commit_have_distinct_outcomes(make_manager):
    manager = make_manager()
    job_id = manager.submit(request())["job_id"]
    assert manager.cancel(job_id)["status"] == "cancelled"
    assert not manager.run_once()
    assert all(not node["attempts"] for node in manager.store.get(job_id)["nodes"].values())
    manager.retry(job_id)

    def cancel_after_commit(task, *args, **kwargs):
        result = inline_worker(task, *args, **kwargs)
        if task["node_id"] == "first.report":
            manager.cancel(job_id)
            raise BacktestWorkerError("IPC disappeared after commit")
        return result

    manager._worker = cancel_after_commit
    finished = drain(manager, job_id)
    assert finished["status"] == "succeeded" and finished["cancel_requested"]
    assert manager.store.path(finished["artifact"]["path"]).is_file()


def test_missing_facts_fail_without_reusing_old_output(make_manager):
    manager = make_manager()
    job_id = manager.submit(request())["job_id"]
    path = next(manager.store.path("inputs/snapshot/datasets").rglob("*.parquet"))
    original = path.read_bytes()
    path.write_bytes(original + b"broken")
    result = drain(manager, job_id)
    assert result["status"] == "failed"
    assert result["nodes"]["first.execute"]["attempts"] == []
    assert result["artifact"] is None
    path.write_bytes(original)
    manager.retry(job_id)
    assert drain(manager, job_id)["status"] == "succeeded"


def test_final_commit_is_recovered_when_state_write_did_not_happen(make_manager, monkeypatch):
    import app.services.engine_jobs.manager as module

    manager = make_manager()
    job_id = manager.submit(request())["job_id"]
    original = module.publish_document

    def interrupted(store, path, value, observer):
        original(store, path, value, observer)
        raise OSError("simulated loss after final marker")

    monkeypatch.setattr(module, "publish_document", interrupted)
    manager.run_once()
    manager.run_once()
    with pytest.raises(OSError, match="simulated"):
        manager.run_once()
    assert manager.store.get(job_id)["status"] == "publishing"
    assert manager.shutdown()
    monkeypatch.setattr(module, "publish_document", original)
    restored = make_manager()
    assert drain(restored, job_id)["status"] == "succeeded"
    assert all(len(node["attempts"]) == 1 for node in restored.store.get(job_id)["nodes"].values())


def test_preview_catalog_and_checked_submission(make_manager):
    manager = make_manager()
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    client = TestClient(app)
    catalog = client.get("/api/engine-jobs/inputs")
    assert catalog.status_code == 200, catalog.text
    listing = catalog.json()
    assert listing["catalog_version"] == "1.0" and not listing["truncated"]
    assert [item["path"] for item in listing["runs"]] == ["run"]
    assert [item["path"] for item in listing["snapshots"]] == ["snapshot"]
    assert listing["runs"][0]["snapshot_id"] == listing["snapshots"][0]["snapshot_id"]
    assert listing["runs"][0]["price_view"]["mode"] == "qfq"
    assert listing["runs"][0]["initial_capital"] == "5000"
    assert listing["issues"] == []
    body = request().model_dump(mode="json")
    preview = client.post("/api/engine-jobs/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert manager.store.list() == []
    assert not list(manager.store.path("jobs").iterdir())
    plan_id = preview.json()["plan_id"]
    assert (
        preview.json()["plan"]["inputs"][0]["snapshot_id"] == listing["snapshots"][0]["snapshot_id"]
    )
    wrong = client.post("/api/engine-jobs?expected_plan_id=" + "a" * 64, json=body)
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "DATA_CONFLICT"
    assert manager.store.list() == []
    created = client.post("/api/engine-jobs?expected_plan_id=" + plan_id, json=body)
    assert created.status_code == 200, created.text
    assert created.json()["plan_id"] == plan_id
    repeated = client.post("/api/engine-jobs?expected_plan_id=" + plan_id, json=body)
    assert repeated.json()["job_id"] == created.json()["job_id"]
    assert len(manager.store.list()) == 1


def test_save_price_variant_retains_input_and_runs_checked_new_basis(make_manager):
    from concurrent.futures import ThreadPoolExecutor

    from app.services.engine_jobs.artifacts import read_input_run

    manager = make_manager()
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    client = TestClient(app)
    root = "/api/engine-jobs"
    endpoint = root + "/inputs/price-variants"
    views = {
        item["provider_id"]: item["state"] for item in client.get(root + "/price-views").json()
    }
    assert views == {
        "tivelo.raw": "available",
        "tivelo.qfq": "available",
        "tivelo.hfq": "available",
        "tivelo.pro.dynamic": "not_installed",
    }
    original = client.get(root + "/inputs").json()["runs"][0]
    assert original["controlled_semantics"] == "anchor_cancelled_float64_v1"
    archive, source = read_input_run(manager.store, "run")
    body = dict(
        run="run",
        snapshot="snapshot",
        source_lock_id=source.lock_id,
        price_view=original["price_view"],
    )
    assert client.post(endpoint, json=body).json()["input"] == original
    assert client.post(endpoint, json={**body, "source_lock_id": "0" * 64}).status_code == 409
    assert client.post(endpoint, json={**body, "snapshot": "../snapshot"}).status_code == 422
    assert (
        client.post(
            endpoint, json={**body, "price_view": {"mode": "hfq", "provider_id": "tivelo.hfq"}}
        ).status_code
        == 422
    )
    commercial = dict(
        mode="extension",
        provider_id="tivelo.pro.dynamic",
        factor_definition_id="fixture.cumulative",
        factor_baseline_id="fixture.listing",
    )
    assert (
        client.post(endpoint, json={**body, "price_view": commercial}).json()["detail"]["code"]
        == "EXTENSION_INCOMPATIBLE"
    )
    body["price_view"] = {
        **original["price_view"],
        "mode": "hfq",
        "provider_id": "tivelo.hfq",
        "anchor_at": "2025-01-02T07:00:00Z",
    }
    with ThreadPoolExecutor(max_workers=3) as executor:
        saved = list(executor.map(lambda _: client.post(endpoint, json=body), range(3)))
    assert all(response.status_code == 200 for response in saved), [
        response.text for response in saved
    ]
    assert all(response.json() == saved[0].json() for response in saved)
    derived = saved[0].json()["input"]
    assert (
        derived["path"] == "price-variants/" + derived["lock_id"]
        and derived["lock_id"] != source.lock_id
    )
    assert saved[0].json()["snapshot"] == "snapshot"
    assert read_input_run(manager.store, "run") == (archive, source)
    _, locked = read_input_run(manager.store, derived["path"])
    assert locked.contents.snapshot == source.contents.snapshot
    assert locked.contents.spec.strategy == source.contents.spec.strategy
    assert locked.contents.market_adapter == source.contents.market_adapter
    assert manager.store.list() == []
    job = request(key="price-comparison").model_dump(mode="json")
    job["nodes"][0]["run"] = derived["path"]
    plan = client.post(root + "/preview", json=job)
    assert plan.status_code == 200, plan.text
    created = client.post(root, params={"expected_plan_id": plan.json()["plan_id"]}, json=job)
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    state = drain(manager, job_id)
    assert state["status"] == "succeeded", state["nodes"]
    report = client.get(f"{root}/{job_id}/reports/first").json()["summary"]
    assert report["semantics"]["price_view"]["mode"] == "hfq"
    assert report["semantics"]["lock_id"] == derived["lock_id"]


def test_changed_valid_run_metadata_rejects_the_checked_plan(make_manager, seed):
    from tivelo_backtest.runner import RunRequest, prepare_run

    from app.services.engine_jobs.artifacts import read_input_run

    manager = make_manager()
    checked = manager.preview(request())
    _, lock = read_input_run(manager.store, "run")
    original = RunRequest.model_validate_json((seed / "controlled-request.json").read_bytes())
    changed = RunRequest.model_validate({**original.model_dump(), "initial_capital": "6000"})
    prepare_run(
        changed,
        snapshot=manager.store.path("inputs/snapshot"),
        definition=manager.store.path(
            "inputs/run/definitions/" + lock.contents.spec.strategy.sha256
        ),
        destination=manager.store.path("inputs/replacement-run"),
    )
    # Simulate a user replacing an input directory after checking it.
    original_path = manager.store.path("inputs/run")
    original_path.rename(manager.store.path("inputs/original-run"))
    manager.store.path("inputs/replacement-run").rename(original_path)
    with pytest.raises(JobError, match="重新检查") as rejected:
        manager.submit(request(), expected_plan_id=checked["plan_id"])
    assert rejected.value.code == "DATA_CONFLICT" and manager.store.list() == []
    refreshed = manager.preview(request())
    assert refreshed["plan_id"] != checked["plan_id"]
    assert manager.submit(request(), expected_plan_id=refreshed["plan_id"])["status"] == "queued"


def test_preview_budget_change_requires_another_check(make_manager, monkeypatch):
    import app.services.engine_jobs.manager as module

    manager = make_manager()
    monkeypatch.setattr(module, "_effective_rss_limit_bytes", lambda: 2 * 1024**3)
    checked = manager.preview(request())
    monkeypatch.setattr(module, "_effective_rss_limit_bytes", lambda: 1024**3)
    with pytest.raises(JobError) as error:
        manager.submit(request(), expected_plan_id=checked["plan_id"])
    assert error.value.code == "DATA_CONFLICT"
    assert manager.store.list() == []
    updated = manager.preview(request())
    assert updated["plan"]["effective_budget"]["rss_limit_bytes"] == 1024**3
    assert manager.submit(request(), expected_plan_id=updated["plan_id"])["status"] == "queued"


def test_catalog_is_bounded_and_rejects_symlinks_and_broken_metadata(
    make_manager, tmp_path, monkeypatch
):
    from app.services.engine_jobs import catalog

    manager = make_manager()
    broken = manager.store.path("inputs/broken")
    broken.mkdir()
    (broken / "run-lock.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private-content-must-not-escape")
    manager.store.path("inputs/link").symlink_to(outside, target_is_directory=True)
    listing = catalog.input_catalog(manager.store)
    assert {item["path"]: item["code"] for item in listing["issues"]} == {
        "broken": "INVALID_INPUT",
        "link": "UNSAFE_PATH",
    }
    assert "private-content" not in json.dumps(listing)
    assert listing["runs"] and listing["snapshots"]
    monkeypatch.setattr(catalog, "MAX_ENTRIES", 1)
    assert catalog.input_catalog(manager.store)["truncated"]
    monkeypatch.setattr(catalog, "MAX_ENTRIES", 1024)
    monkeypatch.setattr(catalog, "MAX_METADATA_BYTES", 1)
    limited = catalog.input_catalog(manager.store)
    assert limited["truncated"] and not limited["runs"] and not limited["snapshots"]


def test_api_schema_submission_reports_errors_and_missing_service(make_manager, tmp_path, monkeypatch):
    import errno
    import subprocess
    import sys
    from io import BytesIO
    from zipfile import ZipFile

    from tivelo_backtest.archives import read_result
    from tivelo_backtest.study_archives import read_study

    from app.services.engine_jobs import result_export

    service = make_manager()
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = service
    client = TestClient(app)
    assert client.get("/api/engine-jobs/schema").json() == JobRequest.model_json_schema()
    created = client.post("/api/engine-jobs", json=request(study=True).model_dump(mode="json"))
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    assert (
        client.get(f"/api/engine-jobs/{job_id}/plan").json()["request"]["idempotency_key"]
        == "test-1"
    )
    assert client.get(f"/api/engine-jobs/{job_id}/reports/first").status_code == 409
    assert client.get(f"/api/engine-jobs/{job_id}/results/first.zip").status_code == 409
    assert client.get(f"/api/engine-jobs/{job_id}/results/missing.zip").status_code == 404
    assert client.get("/api/engine-jobs/unknown").status_code == 404
    invalid = request().model_dump(mode="json")
    invalid["nodes"][0]["run"] = "../../secret"
    assert client.post("/api/engine-jobs", json=invalid).status_code == 422
    assert drain(service, job_id)["status"] == "succeeded"
    report = client.get(f"/api/engine-jobs/{job_id}/reports/first")
    assert (
        report.status_code == 200
        and report.json()["summary"]["metrics"]["final_equity"] == "5237.58"
    )
    from app.services.engine_jobs.artifacts import read_input_run

    _, locked = read_input_run(service.store, "run")
    semantics = report.json()["summary"]["semantics"]
    assert semantics["start"] == locked.contents.spec.start.isoformat()
    assert semantics["end"] == locked.contents.spec.end.isoformat()
    assert semantics["lock_id"] == locked.lock_id
    assert semantics["universe_count"] == len(locked.contents.spec.universe)
    assert semantics["strategy"] == locked.contents.spec.strategy.model_dump(mode="json")
    assert semantics["semantics_version"] == locked.contents.spec.semantics_version

    service.store.path("inputs/run").rename(tmp_path / "unavailable-run")
    service.store.path("inputs/snapshot").rename(tmp_path / "unavailable-snapshot")
    for node, read in (("first", read_result), ("second", read_study)):
        published = client.get(f"/api/engine-jobs/{job_id}/reports/{node}").json()
        archive = service.store.path(published["execution"]["path"])
        (archive / "unlisted.txt").write_text("local diagnostic", encoding="utf-8")
        response = client.get(f"/api/engine-jobs/{job_id}/results/{node}.zip")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["cache-control"] == "private, no-store"
        assert int(response.headers["content-length"]) == len(response.content)
        assert published["execution"]["identity"] + ".zip" in response.headers["content-disposition"]
        destination = tmp_path / "downloads" / node
        with ZipFile(BytesIO(response.content)) as downloaded:
            assert json.loads(downloaded.read("report.json")) == published
            manifest = json.loads(downloaded.read("output/manifest.json"))
            assert set(downloaded.namelist()) == {
                "report.json", "output/manifest.json",
                *(f"output/{file['path']}" for file in manifest["contents"]["files"]),
            }
            downloaded.extractall(destination)
        assert read(destination / "output") == read(archive)

    exported = tmp_path / "downloads/first/output"
    replayed = tmp_path / "replayed"
    command = [
        sys.executable, "-I", "-m", "tivelo_backtest.cli", "result-replay",
        "--archive", str(exported), "--output", str(replayed),
    ]
    missing = subprocess.run(
        [*command, "--snapshot", str(tmp_path / "missing")], text=True, capture_output=True
    )
    assert missing.returncode == 1
    assert json.loads(missing.stderr)["code"] == "SNAPSHOT_NOT_FOUND"
    assert not replayed.exists()
    replay = subprocess.run(
        [*command, "--snapshot", str(tmp_path / "unavailable-snapshot")],
        text=True,
        capture_output=True,
    )
    assert replay.returncode == 0, replay.stderr
    assert (replayed / "manifest.json").read_bytes() == (exported / "manifest.json").read_bytes()

    result_url = f"/api/engine-jobs/{job_id}/results/first.zip"

    def no_space():
        raise OSError(errno.ENOSPC, "test disk full")

    with monkeypatch.context() as failure:
        failure.setattr(result_export, "TemporaryFile", no_space)
        rejected = client.get(result_url)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "PUBLICATION_FAILED"
    orders = service.store.path(report.json()["execution"]["path"] + "/orders.parquet")
    contents = orders.read_bytes()
    orders.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
    rejected = client.get(result_url)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "ARTIFACT_CORRUPT"
    assert client.get(f"/api/engine-jobs/{job_id}/reports/first").json() == report.json()
    assert len(client.get("/api/engine-jobs").json()["items"]) == 1
    app.state.engine_job_manager = None
    assert client.get("/api/engine-jobs").status_code == 503
    assert client.get("/api/engine-jobs/inputs").status_code == 503
    assert (
        client.post("/api/engine-jobs/preview", json=request().model_dump(mode="json")).status_code
        == 503
    )


def test_effective_memory_policy_is_frozen_on_submission(make_manager, monkeypatch):
    import app.services.engine_jobs.manager as module

    monkeypatch.setattr(module, "_effective_rss_limit_bytes", lambda: 1024**3)
    manager = make_manager()
    spec = request()
    job_id = manager.submit(spec)["job_id"]
    assert manager.store.plan(job_id).budget.rss_limit_bytes == 1024**3
    monkeypatch.setattr(module, "_effective_rss_limit_bytes", lambda: 2 * 1024**3)
    assert manager.submit(spec)["job_id"] == job_id
    other = manager.submit(request(key="new-policy"))["job_id"]
    assert manager.store.plan(other).budget.rss_limit_bytes == 2 * 1024**3
    manager.cancel(job_id)
    manager.retry(job_id)
    assert manager.store.plan(job_id).budget.rss_limit_bytes == 1024**3


def test_real_spawn_rss_rejection_stops_process_and_preserves_failure(make_manager, monkeypatch):
    import psutil

    import app.backtest.worker as module

    manager = make_manager(worker_runner=module.run_worker_task)
    job_id = manager.submit(request())["job_id"]
    pids = []

    def sampled_rss(pid):
        assert psutil.Process(pid).is_running()
        pids.append(pid)
        return 20 * 1024**3

    monkeypatch.setattr(module, "_process_rss_bytes", sampled_rss)
    finished = drain(manager, job_id)
    assert finished["status"] == "failed"
    assert finished["nodes"]["first.validate"]["attempts"][0]["error"] == {"code": "RESOURCE_LIMIT"}
    assert pids and all(not psutil.pid_exists(pid) for pid in pids)
    assert finished["artifact"] is None


def blocking_request(manager, seed, timeout):
    from tivelo_backtest.definitions import seal_definition
    from tivelo_backtest.runner import RunRequest, prepare_run
    from tivelo_contracts.base import canonical_bytes

    from app.services.engine_jobs.models import ExecutionBudget

    source = manager.store.path("inputs/blocking-source")
    source.mkdir()
    (source / "strategy.py").write_text(
        "import time\ndef build(*, parameters_json, dependencies):\n    time.sleep(60)\n",
        encoding="utf-8",
    )
    bundle = seal_definition(
        source,
        definition_id="test.blocking",
        version="1",
        kind="strategy",
        entrypoint="strategy:build",
        files=("strategy.py",),
    )
    (source / "definition.json").write_bytes(canonical_bytes(bundle))
    prepared = RunRequest.model_validate_json((seed / "request.json").read_bytes())
    prepare_run(
        prepared,
        snapshot=manager.store.path("inputs/snapshot"),
        definition=source,
        destination=manager.store.path("inputs/blocking-run"),
    )
    return JobRequest(
        idempotency_key="blocking",
        nodes=(ResearchNode(node_id="first", run="blocking-run", snapshot="snapshot"),),
        budget=ExecutionBudget(timeout_seconds=timeout),
    )


def test_real_spawn_timeout_stops_noncooperative_strategy(make_manager, seed):
    from app.backtest.worker import run_worker_task

    manager = make_manager(worker_runner=run_worker_task)
    spec = blocking_request(manager, seed, 2)
    job_id = manager.submit(spec)["job_id"]
    result = drain(manager, job_id)
    assert result["nodes"]["first.validate"]["status"] == "succeeded", result
    assert result["status"] == "failed"
    assert result["nodes"]["first.execute"]["attempts"][0]["error"] == {"code": "RESOURCE_LIMIT"}
    assert result["nodes"]["first.report"]["attempts"] == [] and result["artifact"] is None


def test_cancel_running_process_stops_before_report_and_releases_capacity(
    make_manager, seed, monkeypatch
):
    import psutil

    import app.backtest.worker as module

    manager = make_manager(worker_runner=module.run_worker_task)
    job_id = manager.submit(blocking_request(manager, seed, 30))["job_id"]
    running = threading.Event()
    sampled = module._process_rss_bytes
    pids = []

    def probe(pid):
        value = sampled(pid)
        if manager.store.get(job_id)["nodes"]["first.execute"]["status"] == "running":
            assert psutil.Process(pid).is_running()
            pids.append(pid)
            running.set()
        return value

    monkeypatch.setattr(module, "_process_rss_bytes", probe)
    results = []
    thread = threading.Thread(target=lambda: results.append(drain(manager, job_id)))
    thread.start()
    try:
        assert running.wait(10), manager.store.get(job_id)
        manager.cancel(job_id)
        thread.join(15)
        assert not thread.is_alive()
        assert results[0]["status"] == "cancelled", results
        assert pids and all(not psutil.pid_exists(pid) for pid in pids)
        assert results[0]["nodes"]["first.report"]["attempts"] == []
        assert results[0]["artifact"] is None
    finally:
        manager.cancel(job_id)
        thread.join(15)


def test_cancel_between_parent_check_and_dispatch_remains_cancelled(make_manager, monkeypatch):
    import app.services.engine_jobs.manager as module

    manager = make_manager()
    job_id = manager.submit(request())["job_id"]
    assert manager.run_once()
    verify = module.verify_completed

    def cancel_before_dispatch(store, plan, name):
        artifact = verify(store, plan, name)
        manager.cancel(job_id)
        return artifact

    monkeypatch.setattr(module, "verify_completed", cancel_before_dispatch)
    assert manager.run_once()
    state = manager.store.get(job_id)
    assert state["status"] == "cancelled"
    assert state["nodes"]["first.execute"]["attempts"] == []
    assert state["nodes"]["first.validate"]["status"] == "succeeded"


def test_scheduler_shutdown_keeps_owner_until_active_work_exits(make_manager):
    dispatched, release = threading.Event(), threading.Event()

    def held_worker(task, callback, cancellation, **limits):
        if task["node_id"] == "first.execute":
            dispatched.set()
            assert release.wait(10)
            assert cancellation.is_set()
            raise JobError("CANCELLED", "scheduler stopped")
        return inline_worker(task, callback, cancellation, **limits)

    manager = make_manager(worker_runner=held_worker, autostart=True)
    job_id = manager.submit(request())["job_id"]
    try:
        assert dispatched.wait(10)
        assert manager.shutdown(timeout=0.01) is False
        with pytest.raises(MiningProcessLockError):
            make_manager()
    finally:
        release.set()
        assert manager.shutdown(timeout=10)
    state = manager.store.get(job_id)
    assert state["status"] == "interrupted"
    assert state["nodes"]["first.validate"]["status"] == "succeeded"
    assert state["nodes"]["first.report"]["attempts"] == []
    restored = make_manager()
    restored.retry(job_id)
    result = drain(restored, job_id)
    assert result["status"] == "succeeded"
    assert len(result["nodes"]["first.validate"]["attempts"]) == 1


def preparing_request(manager, seed, *, key="prepare:1"):
    import pyarrow.parquet as pq
    from tivelo_backtest.archives import read_run
    from tivelo_backtest.runner import RunRequest
    from tivelo_contracts.artifacts import SnapshotBundle
    from tivelo_data.providers import SyncRequest
    from tivelo_data.records import model_for

    from app.services.engine_jobs.preparation_models import LocalDataInput, PreparationRequest

    directory = manager.store.path("inputs/imports")
    directory.mkdir()
    bundle = SnapshotBundle.model_validate_json((seed / "portable/bundle.json").read_bytes())
    sources = []
    for index, dataset in enumerate(bundle.datasets):
        contents = dataset.contents
        model = model_for(contents.record_type)
        lines = []
        for artifact in contents.artifacts:
            for row in pq.read_table(seed / "portable" / artifact.path).to_pylist():
                lines.append(model.model_validate(row).model_dump_json())
        (directory / f"{index}.jsonl").write_text("\n".join(lines), encoding="utf-8")
        scope = contents.scope
        sources.append(
            LocalDataInput(
                path=f"imports/{index}.jsonl",
                source_id=contents.source_ids[0],
                knowledge_basis=contents.knowledge_basis,
                bar_convention_id=scope.bar_convention_id,
                coverage=contents.coverage,
                request=SyncRequest(
                    dataset_id=contents.dataset_id,
                    record_type=contents.record_type,
                    market_id=scope.market_id,
                    asset_class=scope.asset_class,
                    instrument_ids=scope.instrument_ids,
                    start=scope.start,
                    end=scope.end,
                    timeframe=scope.timeframes[0] if scope.timeframes else None,
                    price_basis=contents.price_basis,
                ),
            )
        )
    lock, _ = read_run(seed / "controlled-run")
    shutil.copytree(
        seed / "controlled-run/definitions", manager.store.path("inputs/import-definitions")
    )
    run = RunRequest.model_validate_json((seed / "controlled-request.json").read_bytes())
    return JobRequest(
        idempotency_key=key,
        nodes=(
            ResearchNode(
                node_id="daily",
                prepare=PreparationRequest(
                    datasets=tuple(sources),
                    definition="import-definitions/" + lock.contents.spec.strategy.sha256,
                    dependency_root="import-definitions",
                    run=run,
                ),
            ),
        ),
    )


def test_data_preparation_runs_full_chain_and_captures_definition(make_manager, seed):
    from app.backtest.worker import run_worker_task

    manager = make_manager(worker_runner=run_worker_task)
    specification = preparing_request(manager, seed)
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    client = TestClient(app)
    submitted = client.post("/api/engine-jobs", json=specification.model_dump(mode="json"))
    assert submitted.status_code == 200, submitted.text
    job = submitted.json()
    assert client.get(f"/api/engine-jobs/{job['job_id']}/plan").json()["plan_version"] == "1.1"
    conflicting = specification.model_dump(mode="json")
    conflicting["nodes"][0]["run"] = "run"
    assert client.post("/api/engine-jobs", json=conflicting).status_code == 422
    source = manager.store.path("inputs/" + specification.nodes[0].prepare.definition)
    (source / "program.json").write_text("{}", encoding="utf-8")
    assert manager.submit(specification)["job_id"] == job["job_id"]
    finished = drain(manager, job["job_id"], maximum=64)
    assert finished["status"] == "succeeded", finished
    assert len(finished["nodes"]) == 15
    assert all(
        node["attempts"][0]["worker"]["pid"] != os.getpid() for node in finished["nodes"].values()
    )
    report = ResearchReport.model_validate_json(
        manager.store.path(finished["nodes"]["daily.report"]["artifact"]["path"]).read_bytes()
    )
    assert report.summary["metrics"]["final_equity"] == "5237.58"
    assert client.get(
        f"/api/engine-jobs/{job['job_id']}/reports/daily"
    ).json() == report.model_dump(mode="json")
    assert manager.shutdown()
    restored = make_manager()
    assert restored.submit(specification) == finished


@pytest.mark.parametrize("record_type", ("order_book_observation", "trade_print"))
def test_portable_market_events_are_published_by_the_research_worker(make_manager, record_type):
    from tivelo_backtest.snapshot import PortableSnapshotReader
    from tivelo_contracts.order_books import OrderBookObservation, OrderBookQuery
    from tivelo_contracts.trade_prints import TradePrint, TradePrintQuery

    from app.backtest.worker import run_worker_task

    manager = make_manager(worker_runner=run_worker_task)
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    at, corrected_at, end = "2025-01-03T02:00:00Z", "2025-01-03T02:01:00Z", "2025-01-03T02:02:00Z"
    base = dict(
        instrument_id="A",
        event_at=at,
        trading_date="2025-01-03",
        session_id="morning",
        price_basis="raw",
        quote_currency="CNY",
        available_at=at,
        ingested_at=end,
        source_id="book-fixture",
        revision_id="first",
    )
    if record_type == "trade_print":
        first = TradePrint(
            **base,
            sequence=1,
            raw_price="2.01",
            quantity="60",
            quantity_unit="share",
            aggressor_side="sell",
        )
        amended = {"quantity": "40"}
    else:
        first = OrderBookObservation(
            **base,
            state="continuous",
            best_bid="1.99",
            best_ask="2.01",
            last_trade=None,
        )
        amended = {"best_ask": "2.02"}
    correction = type(first).model_validate(
        {**first.model_dump(), **amended, "available_at": corrected_at, "revision_id": "second"}
    )
    manager.store.path("inputs/books.jsonl").write_text(
        first.model_dump_json() + "\n" + correction.model_dump_json() + "\n",
        encoding="utf-8",
    )
    request = {
        "idempotency_key": "book-capture",
        "nodes": [
            {
                "node_id": "book",
                "capture": {
                    "datasets": [
                        {
                            "path": "books.jsonl",
                            "source_id": "book-fixture",
                            "knowledge_basis": "historical_versions",
                            "request": {
                                "dataset_id": "book",
                                "record_type": record_type,
                                "market_id": "cn.equity",
                                "asset_class": "equity",
                                "instrument_ids": ["A"],
                                "start": at,
                                "end": end,
                            },
                        }
                    ]
                },
            }
        ],
    }
    client = TestClient(app)
    root = "/api/engine-jobs"
    preview = client.post(root + "/preview", json=request)
    assert preview.is_success, preview.text
    plan_id = preview.json()["plan_id"]
    submitted = client.post(root, json=request, params={"expected_plan_id": plan_id})
    assert submitted.is_success, submitted.text
    job_id = submitted.json()["job_id"]
    state = drain(manager, job_id)
    assert state["status"] == "succeeded", state["nodes"]
    assert all(
        node["attempts"][0]["worker"]["pid"] != os.getpid() for node in state["nodes"].values()
    )
    received = client.get(f"{root}/{job_id}/snapshots/book")
    assert received.is_success, received.text
    artifact = received.json()
    with PortableSnapshotReader(
        manager.store.path(artifact["path"]), snapshot_id=artifact["snapshot_id"]
    ) as reader:
        (dataset,) = reader.manifest.contents.datasets
        query = {"dataset": dataset, "instrument_ids": ("A",), "start": at, "end": end}
        if record_type == "trade_print":
            query["trading_date"] = "2025-01-03"
            model, read = TradePrintQuery, reader.trade_prints
        else:
            model, read = OrderBookQuery, reader.order_books
        assert read(model(**query, as_of=at)) == (first,)
        assert read(model(**query, as_of=corrected_at)) == (correction,)


@pytest.mark.parametrize("changed", [False, True])
def test_preparation_opens_only_after_admission_and_closes_each_source_once(make_manager, seed, monkeypatch, changed):
    from tivelo_contracts import ErrorCode, TiveloError
    from tivelo_data.providers import SyncContext, local_context

    from app.services.engine_jobs import preparation

    manager = make_manager()
    spec = preparing_request(manager, seed)
    created = manager.submit(spec)
    locked = manager.store.plan(created["job_id"]).inputs[0].preparation.sources[0]
    opened, closed = [], []
    create = preparation.source_provider

    def track(*args, **kwargs):
        provider = create(*args, **kwargs)
        opened.append(provider)
        close = provider.close

        def close_once():
            closed.append(provider)
            close()

        provider.close = close_once
        if changed:
            provider.configuration_digest = "c" * 64
        return provider

    monkeypatch.setattr(preparation, "source_provider", track)
    resolver = preparation._JobSourceResolver(manager.store, locked)
    cancelled = SyncContext(local_context().resolve_credentials, lambda: True, lambda *_: None)
    with pytest.raises(TiveloError) as error:
        preparation.engine(manager.store, resolver).sync(locked.plan, cancelled)
    assert error.value.code == ErrorCode.CANCELLED and not opened
    assert manager.run_once()
    state = manager.store.get(created["job_id"])["nodes"]["daily.sync_00"]
    assert state["status"] == ("failed" if changed else "succeeded")
    if changed:
        assert state["attempts"][0]["error"] == {"code": "DATA_CONFLICT"}
    assert len(opened) == 1 and closed == opened


def test_preparation_retry_keeps_published_sources_and_rejects_changed_input(make_manager, seed):
    manager = make_manager()
    specification = preparing_request(manager, seed)
    job_id = manager.submit(specification)["job_id"]
    assert manager.run_once()  # First source staged.
    assert manager.run_once()  # First source published.
    source = manager.store.path("inputs/" + specification.nodes[0].prepare.datasets[1].path)
    original = source.read_bytes()
    source.write_text("{}", encoding="utf-8")
    rejected = drain(manager, job_id, maximum=64)
    assert rejected["status"] == "failed"
    assert rejected["nodes"]["daily.sync_01"]["attempts"][-1]["error"]["code"] == "DATA_CONFLICT"
    assert rejected["nodes"]["daily.snapshot"]["attempts"] == []
    source.write_bytes(original)
    manager.retry(job_id)
    completed = drain(manager, job_id, maximum=64)
    assert completed["status"] == "succeeded", completed
    assert len(completed["nodes"]["daily.publish_00"]["attempts"]) == 1
    assert len(completed["nodes"]["daily.sync_01"]["attempts"]) == 2


def test_empty_source_without_explicit_event_coverage_blocks_backtest(make_manager, seed):
    manager = make_manager()
    original = preparing_request(manager, seed)
    body = original.model_dump(mode="json")
    for source in body["nodes"][0]["prepare"]["datasets"]:
        if source["request"]["record_type"] == "corporate_action":
            source["coverage"] = None
    job_id = manager.submit(JobRequest.model_validate(body))["job_id"]
    failed = drain(manager, job_id, maximum=64)
    assert failed["status"] == "failed", failed
    assert (
        failed["nodes"]["daily.prepare"]["attempts"][-1]["error"]["code"]
        == "DATA_COVERAGE_INSUFFICIENT"
    )
    assert failed["nodes"]["daily.execute"]["attempts"] == []


def test_published_dataset_survives_lost_ack_and_snapshot_pin_is_released(make_manager, seed):
    def lose_ack(task, callback, cancellation, **limits):
        result = inline_worker(task, callback, cancellation, **limits)
        if task["node_id"] == "daily.publish_00":
            raise BacktestWorkerError("lost publication acknowledgement")
        return result

    manager = make_manager(worker_runner=lose_ack)
    job_id = manager.submit(preparing_request(manager, seed))["job_id"]
    completed = drain(manager, job_id, maximum=64)
    assert completed["status"] == "succeeded", completed
    assert len(completed["nodes"]["daily.publish_00"]["attempts"]) == 1
    from app.services.engine_jobs.preparation import engine

    with engine(manager.store).store.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM refs WHERE owner LIKE 'engine-job:%'"
            ).fetchone()[0]
            == 0
        )


def test_cancel_during_workbench_snapshot_export_releases_reference(
    make_manager, seed, monkeypatch
):
    from tivelo_contracts.errors import ErrorCode, TiveloError
    from tivelo_data import DataEngine

    from app.services.engine_jobs.preparation import engine

    manager = make_manager()
    snapshot_ids = []
    original_export = DataEngine.export_snapshot

    def interrupt(data, snapshot_id, destination, *, observer):
        original_progress = observer.progress

        def cancel_after_copy(done, total, phase):
            original_progress(done, total, phase)
            if phase == "exporting" and not snapshot_ids:
                with pytest.raises(TiveloError) as protected:
                    data.store.delete_snapshot(snapshot_id)
                assert protected.value.code == ErrorCode.REFERENCED_ARTIFACT
                snapshot_ids.append(snapshot_id)
                manager.cancel(job_id)

        observer.progress = cancel_after_copy
        return original_export(data, snapshot_id, destination, observer=observer)

    monkeypatch.setattr(DataEngine, "export_snapshot", interrupt)
    job_id = manager.submit(preparing_request(manager, seed))["job_id"]
    state = drain(manager, job_id, maximum=64)
    assert state["status"] == "cancelled"
    assert state["nodes"]["daily.snapshot"]["artifact"] is None
    (snapshot_id,) = snapshot_ids
    engine(manager.store).store.delete_snapshot(snapshot_id)


def test_workbench_and_cli_share_witnessed_continuous_run(make_manager, tmp_path):
    import subprocess
    import sys
    from copy import deepcopy
    from datetime import date, timedelta
    from decimal import Decimal

    from test_tivelo_execution_integration import at, compiled_fee_policy
    from tivelo_backtest.archives import read_result
    from tivelo_backtest.definitions import seal_definition
    from tivelo_contracts.base import canonical_bytes
    from tivelo_contracts.data import AdjustmentFactor, Bar, OpeningObservation
    from tivelo_contracts.order_books import OrderBookObservation
    from tivelo_contracts.trade_prints import TradePrint
    from tivelo_market_cn.adapter import CNMarketProfile, create_rules
    from tivelo_market_cn.profile_presets import CashEquityProfileRequest, compile_market_adapter

    from app.backtest.worker import run_worker_task

    manager = make_manager(worker_runner=run_worker_task)
    market_spec = compile_market_adapter(
        CashEquityProfileRequest(
            venue_id="XSHG",
            start=date(2025, 1, 3),
            end=date(2025, 1, 6),
            broker_policy=compiled_fee_policy(("handling", "regulatory")).inputs.policy,
            profile_format="tivelo-cn-profile-v5",
            observed_book_max_age_ms=500,
        )
    )
    profile = CNMarketProfile.model_validate_json(market_spec.configuration_json)
    _, original = configured_market()
    (classification,) = profile.configuration.instrument_classes
    security = classification.bind(
        original.model_copy(update={"calendar_id": profile.calendar.contents.calendar_id})
    )
    market = create_rules(configuration_json=market_spec.configuration_json)
    friday, monday = market.execution_rules.trading_days(
        date(2025, 1, 3), date(2025, 1, 6), as_of=at(6, 8)
    )
    buy, arrived = at(3, 2), at(6, 2)
    second = buy + timedelta(seconds=2)
    sell = arrived + timedelta(milliseconds=1)
    sold = arrived + timedelta(seconds=1)
    big_sequence = 2**53 + 1
    currency = {"quote_currency": "CNY", "revision_id": "first"}

    def event(moment, **fields):
        return dict(
            instrument_id="A",
            event_at=moment,
            trading_date=moment.date(),
            session_id=market.continuous_session_id(moment),
            ingested_at=at(7, 0),
            source_id="fixture",
            **currency,
            **fields,
        )

    opens = [
        OpeningObservation(
            **event(day.open_at, available_at=day.open_at),
            raw_open="2",
            state="tradable",
            reference_price="2",
            reference_kind="exchange_ex_rights",
        )
        for day in (friday, monday)
    ]
    book = [
        OrderBookObservation(
            **event(moment - timedelta(milliseconds=250), available_at=moment),
            price_basis="raw",
            state="continuous",
            best_bid="1.99",
            best_ask="2.01",
            last_trade=None,
        )
        for moment in (buy, sell)
    ]
    book.append(
        book[0].model_copy(
            update={
                "available_at": buy + timedelta(milliseconds=1),
                "revision_id": "late-correction",
                "best_ask": Decimal("2.00"),
            }
        )
    )
    tape = [
        TradePrint(
            **event(moment, available_at=known),
            price_basis="raw",
            sequence=seq,
            raw_price=price,
            quantity=qty,
            quantity_unit="share",
            aggressor_side=side,
        )
        for moment, known, seq, price, qty, side in (
            (buy + timedelta(seconds=1), arrived, 8, "2", 60, "sell"),
            (second, second, big_sequence, "2", 30, "sell"),
            (sold, sold, 1, "2.05", 90, "buy"),
        )
    ]
    bars = [
        Bar(
            instrument_id="A",
            timeframe="1m",
            bar_start=end - timedelta(minutes=1),
            bar_end=end,
            trading_date=end.date(),
            session_id="minute",
            open="2",
            high="2",
            low="2",
            close="2",
            volume="100",
            quantity_unit="share",
            price_basis="raw",
            available_at=end,
            ingested_at=end,
            source_id="fixture",
            **currency,
        )
        for end in (buy, friday.close_at, arrived, monday.close_at)
    ]
    factors = [
        AdjustmentFactor(
            factor_id=f"factor-{n}",
            instrument_id="A",
            effective_at=effective,
            available_at=effective,
            value=value,
            definition_id="cumulative",
            baseline_id="anchor",
            factor_kind="cumulative_forward",
            source_id="fixture",
            revision_id="1",
        )
        for n, (effective, value) in enumerate(((at(1, 2), "2"), (friday.open_at, "4")))
    ]

    def dataset(kind, rows, **options):
        path = f"{kind}.jsonl"
        manager.store.path(f"inputs/{path}").write_bytes(
            b"".join(
                canonical_bytes(row.model_copy(update={"source_id": kind})) + b"\n" for row in rows
            )
        )
        return {
            "path": path,
            "source_id": kind,
            "knowledge_basis": "historical_versions",
            "request": {
                "dataset_id": kind,
                "record_type": kind,
                "market_id": "cn.equity",
                "asset_class": "equity",
                "instrument_ids": ["A"],
                "start": at(1, 0).isoformat(),
                "end": at(6, 8).isoformat(),
                **options,
            },
        }

    actions = dataset("corporate_action", [])
    actions["coverage"] = {
        "status": "empty",
        "requested_units": 1,
        "completed_units": 1,
        "row_count": 0,
        "issues": [{"instrument_id": "A", "reason": "no_events", "detail": "checked fixture"}],
    }
    bar_data = dataset("bar", bars, timeframe="1m", price_basis="raw")
    bar_data["bar_convention_id"] = "fixture.minute"
    datasets = [
        dataset("adjustment_factor", factors),
        bar_data,
        actions,
        dataset("instrument", [security]),
        dataset("opening_observation", opens),
        dataset("order_book_observation", book),
        dataset("trade_print", tape),
    ]
    datasets.sort(key=lambda d: d["request"]["dataset_id"])
    source = manager.store.path("inputs/continuous-strategy")
    source.mkdir()
    (source / "strategy.py").write_text(
        """
from decimal import Decimal
from tivelo_contracts.execution import PriceValue, Signal, SignalBatch

class Strategy:
    def decide(self, context):
        assert all(row.available_at <= context.now for row in context.history)
        scale, = context.price_scales
        cash, = context.portfolio.cash
        selling = context.now.day == 6
        if selling:
            position, = context.portfolio.positions
            assert position.sellable_quantity == 90
            assert cash.available == Decimal("35.10")
        else:
            assert not context.portfolio.positions
        limit = Decimal("2.00" if selling else "2.11") / scale.raw_multiplier
        return SignalBatch(decision_at=context.now, signals=(Signal(
            instrument_id="A", decision_at=context.now, side="sell" if selling else "buy",
            source_id="fixture", score="1", limit_quantity="90" if selling else "100",
            limit_price=PriceValue(value=limit, scale=scale),
        ),))

def build(*, parameters_json, dependencies):
    return Strategy()
""",
        encoding="utf-8",
    )
    (source / "definition.json").write_bytes(
        canonical_bytes(
            seal_definition(
                source,
                definition_id="strategy.continuous-fixture",
                version="1",
                kind="strategy",
                entrypoint="strategy:build",
                files=("strategy.py",),
            )
        )
    )

    def price_view(mode):
        if mode == "raw":
            return {"provider_id": "tivelo.raw", "mode": mode}
        return {
            "provider_id": f"tivelo.{mode}",
            "mode": mode,
            "factor_definition_id": "cumulative",
            "factor_baseline_id": "anchor",
            "anchor_at": (at(1, 2) if mode == "hfq" else friday.open_at).isoformat(),
        }

    run = {
        "universe": ["A"],
        "start": "2025-01-03",
        "end": "2025-01-06",
        "market": market_spec.model_dump(mode="json"),
        "account": {"currency": "CNY", "order_expiry": "confirmed_end_of_day"},
        "initial_capital": "235",
        "price_view": price_view("raw"),
        "execution": {
            "timeframe": "1m",
            "signal_at": "scheduled",
            "fill_at": "witnessed_trade",
            "order_expiry": "confirmed_end_of_day",
            "continuous": {
                "book_dataset_id": "order_book_observation",
                "trade_dataset_id": "trade_print",
                "observe_at": [second.isoformat(), sold.isoformat()],
                "decide_at": [buy.isoformat(), sell.isoformat()],
                "sessions": [
                    {
                        "instrument_id": "A",
                        "trading_date": "2025-01-03",
                        "sequences": ["8", str(big_sequence)],
                        "confirmed_through_at": arrived.isoformat(),
                    },
                    {
                        "instrument_id": "A",
                        "trading_date": "2025-01-06",
                        "sequences": ["1"],
                        "confirmed_through_at": monday.close_at.isoformat(),
                    },
                ],
            },
        },
    }
    request = {
        "idempotency_key": "continuous",
        "nodes": [
            {
                "node_id": "tape",
                "prepare": {
                    "definition": "continuous-strategy",
                    "datasets": datasets,
                    "run": run,
                },
            }
        ],
    }
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    client = TestClient(app)
    root = "/api/engine-jobs"
    preview = client.post(root + "/preview", json=request)
    assert preview.is_success, preview.text
    response = client.post(
        root, json=request, params={"expected_plan_id": preview.json()["plan_id"]}
    )
    assert response.is_success, response.text
    job_id = response.json()["job_id"]
    state = drain(manager, job_id, maximum=30)
    assert state["status"] == "succeeded", state
    report = client.get(f"{root}/{job_id}/reports/tape").json()
    assert report["summary"]["semantics"]["execution"]["fill_at"] == "witnessed_trade"
    profile_parameters = report["summary"]["semantics"]["market_profile"]["parameters_json"]
    assert json.loads(profile_parameters) == {"observed_book_max_age_ms": 500}
    assert report["summary"]["metrics"]["filled_orders"] == 2
    output = manager.store.path(state["nodes"]["tape.execute"]["artifact"]["path"])
    pinned, result, metrics = read_result(output)
    assert metrics.final_equity == Decimal("210.01")
    assert [entry.quantity_delta for entry in result.ledger] == [60, 30, -90]
    assert [entry.at for entry in result.ledger] == [arrived, arrived, sold]
    assert sum(order.status == "expired" for order in result.orders) == 1
    assert result.final_portfolio.positions == ()
    snapshot = manager.store.path(state["nodes"]["tape.snapshot"]["artifact"]["path"])

    def cli(*arguments):
        run = subprocess.run(
            [sys.executable, "-I", "-m", "tivelo_backtest.cli", *map(str, arguments)],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout)

    for mode in ("raw", "qfq", "hfq"):
        request_path = tmp_path / f"{mode}.json"
        request_path.write_text(
            json.dumps({**run, "price_view": price_view(mode)}), encoding="utf-8"
        )
        run_directory, output_directory = (
            tmp_path / f"{mode}-{kind}" for kind in ("run", "result")
        )
        cli(
            "prepare",
            "--request",
            request_path,
            "--definition",
            source,
            "--snapshot",
            snapshot,
            "--output",
            run_directory,
        )
        cli("run", "--run", run_directory, "--snapshot", snapshot, "--output", output_directory)
        lock, standalone, totals = read_result(output_directory)
        assert totals == metrics and standalone.ledger == result.ledger
        assert standalone.orders == result.orders
        if mode == "raw":
            assert (lock, standalone) == (pinned, result)

    missing = deepcopy(run)
    missing["execution"]["continuous"]["sessions"][0]["sequences"].append(str(big_sequence + 1))
    response = client.post(
        root,
        json={
            "idempotency_key": "continuous-missing",
            "nodes": [
                {
                    "node_id": "tape",
                    "prepare": {
                        "definition": "continuous-strategy",
                        "run": missing,
                        "versions": [
                            ref.model_dump(mode="json")
                            for ref in pinned.contents.snapshot.contents.datasets
                        ],
                    },
                }
            ],
        },
    )
    assert response.is_success, response.text
    bad_id = response.json()["job_id"]
    failure = drain(manager, bad_id)
    assert failure["status"] == "failed"
    assert failure["nodes"]["tape.execute"]["attempts"][0]["error"]["code"] == (
        "DATA_COVERAGE_INSUFFICIENT"
    )
    assert failure["nodes"]["tape.execute"]["artifact"] is None
    assert failure["nodes"]["tape.report"]["artifact"] is None
