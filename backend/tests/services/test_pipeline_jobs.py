from app.services.pipeline_jobs import JobStore


def test_job_with_failed_stages_finishes_degraded(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id = store.create()
    store.start(job_id)

    result = {
        "minute_rows": 0,
        "failed_stages": [{"stage": "sync_minute", "error": "catalog stale"}],
    }
    store.succeed(job_id, result)

    job = store.get(job_id)
    assert job is not None
    assert job["status"] == "degraded"
    assert job["result"] == result
    assert job["error"] is None
    assert store.active_id() is None
