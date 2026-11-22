import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.services.engine_jobs.models import JobPlan, JobRequest, LockedInput, ResearchNode
from app.services.engine_jobs.storage import JobError, JobStore


def plan(*, key="manual-1", nodes=None):
    request = JobRequest(
        idempotency_key=key,
        nodes=nodes or (ResearchNode(node_id="one", run="run", snapshot="snapshot"),),
    )
    return JobPlan(
        request=request,
        inputs=tuple(
            LockedInput(
                request=node,
                run_archive_id="a" * 64,
                lock_id="b" * 64,
                snapshot_id="c" * 64,
                bundle_id="d" * 64,
            )
            for node in request.nodes
        ),
    )


def test_request_dag_and_path_validation():
    for nodes in (
        (ResearchNode(node_id="one", run="run", snapshot="snapshot", depends_on=("missing",)),),
        (ResearchNode(node_id="one", run="run", snapshot="snapshot", depends_on=("one",)),),
        (
            ResearchNode(node_id="one", run="run", snapshot="snapshot", depends_on=("two",)),
            ResearchNode(node_id="two", run="run", snapshot="snapshot", depends_on=("one",)),
        ),
    ):
        with pytest.raises(ValidationError):
            plan(nodes=nodes)
    for path in ("../data", "/tmp/data", "a\\b", "a/./b"):
        with pytest.raises(ValidationError):
            ResearchNode(node_id="one", run=path, snapshot="snapshot")


def test_original_plan_keeps_its_canonical_bytes_and_identity():
    from tivelo_contracts.base import canonical_bytes

    node = {"node_id": "one", "run": "run", "snapshot": "snapshot", "study": None, "depends_on": []}
    original = {
        "plan_version": "1.0",
        "request": {
            "request_version": "1.0",
            "idempotency_key": "manual-1",
            "nodes": [node],
            "budget": {"rss_limit_bytes": 6442450944, "timeout_seconds": 43200, "max_attempts": 3},
        },
        "inputs": [
            {
                "request": node,
                "run_archive_id": "a" * 64,
                "lock_id": "b" * 64,
                "snapshot_id": "c" * 64,
                "bundle_id": "d" * 64,
            }
        ],
    }
    encoded = json.dumps(
        original, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    locked = plan()
    assert canonical_bytes(locked) == encoded
    assert locked.plan_id == hashlib.sha256(encoded).hexdigest()
    assert JobPlan.model_validate_json(encoded) == locked


def test_concurrent_submission_and_restart_retain_one_locked_request(tmp_path):
    first, second = JobStore(tmp_path), JobStore(tmp_path)
    locked = plan()
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(lambda index: (first if index % 2 else second).create(locked), range(16))
        )
    assert {record["job_id"] for record in records} == {locked.plan_id}
    assert len(first.list()) == 1
    restored = JobStore(tmp_path)
    assert restored.plan(locked.plan_id) == locked
    changed = plan(nodes=(ResearchNode(node_id="one", run="other", snapshot="snapshot"),))
    with pytest.raises(JobError, match="请求键"):
        restored.create(changed)
    assert restored.plan(locked.plan_id) == locked


@pytest.mark.parametrize(
    "outcome,code", [("succeeded", None), ("cancelled", "CANCELLED"), ("failed", "RESOURCE_LIMIT")]
)
def test_pending_source_details_belong_only_to_the_failed_decimal_attempt(tmp_path, outcome, code):
    from tivelo_data.repository_diagnostics import RepositoryDecimalIssue, RepositorySourceError

    store, locked = JobStore(tmp_path), plan()
    store.create(locked)
    job_id, stage = locked.plan_id, "one.validate"
    issue = RepositoryDecimalIssue(
        layout="daily_raw_v1",
        trading_date="2025-07-01",
        symbol="600000.SH",
        field="close",
        row_number=2,
        file_sha256="a" * 64,
        reason="fractional_precision",
    )
    attempt = store.begin(job_id, stage)
    failure = RepositorySourceError((issue,))
    store.record_source_failure(job_id, stage, "stale", failure)
    assert "pending_source_error" not in store.get(job_id)["nodes"][stage]["attempts"][-1]
    with store.transaction(job_id) as state:
        state["nodes"][stage]["attempts"][-1]["pending_error"] = "WORKER_INTERRUPTED"
    store.record_source_failure(job_id, stage, attempt["token"], failure)
    assert store.get(job_id)["nodes"][stage]["attempts"][-1]["pending_error"] == "CONTRACT_INVALID"
    if outcome == "cancelled":
        store.cancel(job_id)
    artifact = (
        {"kind": "validation", "identity": "b" * 64, "path": "example"}
        if outcome == "succeeded"
        else None
    )
    error = {"code": code} if code else None
    store.finish(job_id, stage, attempt["token"], outcome, error=error, artifact=artifact)
    saved = store.get(job_id)["nodes"][stage]["attempts"][-1]
    assert (
        saved["error"] == error
        and "pending_source_error" not in saved
        and "pending_error" not in saved
    )
    store.record_source_failure(job_id, stage, attempt["token"], failure)
    assert store.get(job_id)["nodes"][stage]["attempts"][-1] == saved


def test_attempts_are_fenced_and_retries_preserve_history(tmp_path):
    store = JobStore(tmp_path)
    locked = plan()
    store.create(locked)
    attempt = store.begin(locked.plan_id, "one.validate")
    with pytest.raises(JobError, match="上游"):
        store.begin(locked.plan_id, "one.execute")
    assert JobStore(tmp_path).get(locked.plan_id)["status"] == "running"
    store.finish(
        locked.plan_id,
        "one.validate",
        attempt["token"],
        "interrupted",
        error={"code": "WORKER_INTERRUPTED"},
    )
    store.retry(locked.plan_id)
    second = store.begin(locked.plan_id, "one.validate")
    assert second["number"] == 2 and second["token"] != attempt["token"]
    with pytest.raises(JobError, match="失效"):
        store.current(locked.plan_id, "one.validate", attempt["token"])
    with pytest.raises(JobError, match="失效"):
        store.finish(locked.plan_id, "one.validate", attempt["token"], "cancelled")
    history = store.get(locked.plan_id)["nodes"]["one.validate"]["attempts"]
    assert [item["status"] for item in history] == ["interrupted", "running"]


def test_cancel_queued_and_retry_limit(tmp_path):
    store = JobStore(tmp_path)
    locked = plan()
    store.create(locked)
    assert store.cancel(locked.plan_id)["status"] == "cancelled"
    for _ in range(3):
        store.retry(locked.plan_id)
        attempt = store.begin(locked.plan_id, "one.validate")
        store.finish(locked.plan_id, "one.validate", attempt["token"], "failed")
    with pytest.raises(JobError, match="次数"):
        store.retry(locked.plan_id)
    assert len(store.get(locked.plan_id)["nodes"]["one.validate"]["attempts"]) == 3


def test_unknown_schema_and_symlinks_do_not_reinitialize_or_escape(tmp_path):
    store = JobStore(tmp_path)
    with sqlite3.connect(store.database) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(JobError, match="版本"):
        JobStore(tmp_path)
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    external = tmp_path / "elsewhere"
    external.mkdir()
    store.path("inputs/link").symlink_to(external, target_is_directory=True)
    with pytest.raises(JobError, match="符号链接"):
        store.path("inputs/link/secret")
    other = tmp_path / "other"
    other.mkdir()
    (other / "research").symlink_to(external, target_is_directory=True)
    with pytest.raises(JobError, match="符号链接"):
        JobStore(other)
    assert not list(external.iterdir())
