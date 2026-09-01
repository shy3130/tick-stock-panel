import pytest

from app.research.control import watch_full_market_process
from app.research.job_store import FactorJobStore
from app.research.run_store import FactorRunStore, RunStoreError


def test_job_events_and_run_immutability(tmp_path):
    jobs = FactorJobStore(tmp_path)
    rid = "rr-0123456789abcdef"
    jobs.create({"run_id": rid, "job_status": "pending"})
    jobs.append_event(rid, "snapshot", {})
    assert jobs.events(rid)[0]["event_type"] == "snapshot"
    runs = FactorRunStore(tmp_path)
    runs.publish(rid, {"ok": True}, {"raw": True})
    with pytest.raises(RunStoreError):
        runs.publish(rid, {}, {})


def test_run_id_whitelist(tmp_path):
    with pytest.raises(ValueError):
        FactorJobStore(tmp_path).get("../bad")


def test_recovery_marks_pending_and_running_jobs_interrupted(tmp_path):
    jobs = FactorJobStore(tmp_path)
    pending = jobs.create({"run_id": "rr-0123456789abcdef", "job_status": "pending"})
    running = jobs.create({"run_id": "rr-fedcba9876543210", "job_status": "pending"})
    jobs.transition(running["run_id"], "running")

    assert jobs.recover_orphans() == 2
    for run_id in (pending["run_id"], running["run_id"]):
        assert jobs.get(run_id)["job_status"] == "interrupted"
        assert jobs.events(run_id)[-1]["event_type"] == "interrupted"


def test_full_market_watcher_reconciles_rss_hard_exit(tmp_path):
    jobs = FactorJobStore(tmp_path)
    run_id = "rr-0123456789abcdef"
    jobs.create({"run_id": run_id, "job_status": "pending"})

    class Process:
        def wait(self):
            return 75

    thread = watch_full_market_process(Process(), jobs, run_id)
    thread.join(timeout=1)

    record = jobs.get(run_id)
    assert record["job_status"] == "failed"
    assert record["error"]["code"] == "runner_rss_exceeded"
