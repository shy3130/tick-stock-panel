from app.research.adapters import _norm, result_data_status
from app.research.catalog import FACTOR_REGISTRY
from app.research.contracts import PreflightRequest, PreflightResult, RunScopeModel
from app.research.control import create_durable_run
from app.research.job_store import FactorJobStore
from app.research.preflight import preflight
from app.services.research_sealed_data import PublishedCanonicalDailyReader


def test_terminal_job_status_is_not_overwritten(tmp_path):
    store = FactorJobStore(tmp_path)
    run_id = "rr-0123456789abcdef"
    store.create({"run_id": run_id, "job_status": "pending"})
    assert store.transition(run_id, "cancelled") is not None
    assert store.transition(run_id, "completed") is None
    assert store.get(run_id)["job_status"] == "cancelled"


def test_full_market_nested_verdict_is_preserved_by_unified_normalizer():
    result = _norm(
        "arm_comparison",
        {
            "schema": "full-market-research-v1",
            "provenance": {"pinned_reader": {"generation": "g-1"}},
            "verdict": {
                "status": "unavailable",
                "verdict": "unavailable",
                "arms": [{"id": "baseline"}],
                "events": [
                    {
                        "symbol": "000001.SZ",
                        "event_date": "2026-08-28",
                        "arm": "baseline",
                    }
                ],
                "series": {"equity": [{"date": "2026-08-28", "value": 1.0}]},
                "unavailable_reasons": [{"code": "insufficient_oos_samples"}],
                "generation": "g-1",
                "manifest_sha256": "a" * 64,
                "cohort": {"count": 12, "hash": "b" * 64},
                "data_availability": {"canonical": "ready"},
            },
        },
    )

    assert result.status == "unavailable"
    assert result.verdict == "unavailable"
    assert result.payload["arms"] == [{"id": "baseline"}]
    assert result.events[0].symbol == "000001.SZ"
    assert result.series["equity"][0]["value"] == 1.0
    assert result.unavailable_reasons[0]["code"] == "insufficient_oos_samples"
    assert result.provenance["generation"] == "g-1"
    assert result.provenance["manifest_sha256"] == "a" * 64
    assert result.provenance["cohort"]["count"] == 12
    assert result.provenance["data_availability"]["canonical"] == "ready"


def test_verdict_and_data_availability_remain_independent():
    unavailable_with_complete_data = _norm(
        "event_signal",
        {"status": "unavailable", "reason": "insufficient_oos_samples"},
    )
    assert result_data_status(unavailable_with_complete_data, fallback="ready") == "ready"

    censored_data = _norm(
        "event_signal",
        {
            "status": "unavailable",
            "provenance": {"data_availability": {"minutes": "censored", "canonical": "ready"}},
        },
    )
    assert result_data_status(censored_data, fallback="ready") == "censored"


def test_validated_run_starts_with_ready_data_status(tmp_path):
    definition = FACTOR_REGISTRY["n-shape"]
    scope = RunScopeModel(type="symbols", symbols=["000001.SZ"])
    parameters = definition.request_model.model_validate(
        {"start": "2026-01-01", "end": "2026-06-01"}
    )
    checked = PreflightResult(
        ready=True,
        factor_id=definition.id,
        normalized_request=parameters.model_dump(mode="json"),
    )
    jobs = FactorJobStore(tmp_path)

    run_id = create_durable_run(jobs, (definition, scope, checked, parameters))

    assert jobs.get(run_id)["data_status"] == "ready"


def test_preflight_blocks_when_required_source_has_no_pinned_provenance(monkeypatch):
    monkeypatch.setattr(
        PublishedCanonicalDailyReader,
        "from_repository",
        classmethod(lambda cls, repo: object()),
    )

    result = preflight(
        object(),
        PreflightRequest(
            factor_id="n-shape",
            scope=RunScopeModel(type="symbols", symbols=["000001.SZ"]),
            parameters={"start": "2026-01-01", "end": "2026-06-01"},
        ),
    )

    assert result.ready is False
    assert result.sources[0].status == "missing"
    assert result.blocking_reasons[0].code == "canonical_provenance_unavailable"
