from datetime import date

import app.research.worker as worker
from app.research.job_store import FactorJobStore
from app.research.run_store import FactorRunStore


def _use_temp_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "_build_runtime", lambda: (tmp_path, object()))


def test_worker_rejects_invalid_run_id(monkeypatch, tmp_path):
    _use_temp_runtime(monkeypatch, tmp_path)
    assert worker.main(["--run-id", "../escape"]) == 2


def test_worker_rejects_unknown_valid_run_id(monkeypatch, tmp_path):
    _use_temp_runtime(monkeypatch, tmp_path)
    assert worker.main(["--run-id", "rr-0123456789abcdef"]) == 2


def test_worker_rehydrates_persisted_parameter_dates_before_adapter(monkeypatch, tmp_path):
    jobs = FactorJobStore(tmp_path)
    runs = FactorRunStore(tmp_path)
    record = jobs.create(
        {
            "run_id": "rr-0123456789abcdef",
            "factor_id": "macd-arms",
            "profile": "arm_comparison",
            "scope": {"type": "full_market"},
            "parameters": {
                "start": "2024-01-01",
                "end": "2025-01-01",
                "oos_start": "2024-07-01",
            },
        }
    )
    captured = {}

    def fake_run(*_args, parameters, **_kwargs):
        captured.update(parameters)
        return {"status": "unavailable", "reason": "insufficient_oos_samples"}

    monkeypatch.setattr(worker, "run_full_market_research", fake_run)
    monkeypatch.setattr(
        worker.PinnedResearchRepository,
        "bind",
        classmethod(lambda cls, repo, record, factor: repo),
    )

    assert worker.execute_job(record, object(), jobs, runs) == 0
    assert isinstance(captured["start"], date)
    assert isinstance(captured["end"], date)
    assert isinstance(captured["oos_start"], date)
    assert jobs.get(record["run_id"])["job_status"] == "completed"
