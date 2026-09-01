from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.research_runs as runs_api
from app.api.research import router as governance_router
from app.api.research_runs import install_error_handlers, router
from app.research.catalog import FACTOR_REGISTRY
from app.research.contracts import PreflightResult, RunScopeModel
from app.research.job_store import FactorJobStore, new_run_id
from app.research.run_store import FactorRunStore
from app.services.research_registry import ResearchStore


def test_catalog_exposes_all_factors(tmp_path):
    response = _client(tmp_path).get("/api/research/factors")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 19

def test_catalog_returns_nested_latest_run_contract(tmp_path):
    run_id = "rr-0123456789abcdef"
    stored = FactorJobStore(tmp_path).create(
        {
            "run_id": run_id,
            "factor_id": "n-shape",
            "scope": {"type": "symbols", "symbols": ["000001.SZ"]},
            "job_status": "completed",
            "verdict": "unavailable",
            "data_status": "ready",
        }
    )

    response = _client(tmp_path).get("/api/research/factors")

    item = next(row for row in response.json()["items"] if row["id"] == "n-shape")
    assert item["latest_run"] == {
        "run_id": run_id,
        "created_at": stored["created_at"],
        "job_status": "completed",
        "verdict": "unavailable",
    }


def test_factor_detail_exposes_parameter_schema(tmp_path):
    response = _client(tmp_path).get("/api/research/factors/macd-arms")
    assert response.status_code == 200
    assert response.json()["parameter_schema"]["type"] == "object"
    assert "oos_start" in response.json()["parameter_schema"]["properties"]


def test_plural_preflight_endpoint_returns_domain_result(tmp_path):
    response = _client(tmp_path).post(
        "/api/research/preflights",
        json={
            "factor_id": "n-shape",
            "scope": {"type": "symbols", "symbols": ["000001.SZ"]},
            "parameters": {"start": "2026-01-01", "end": "2026-06-01"},
        },
    )

    assert response.status_code == 200
    assert response.json()["factor_id"] == "n-shape"
    assert response.json()["ready"] is False
    assert _client(tmp_path).post("/api/research/preflight", json={}).status_code == 404


def test_run_creation_rechecks_preflight_and_creates_nothing_when_blocked(tmp_path):
    client = _client(tmp_path)
    response = client.post(
        "/api/research/runs",
        json={
            "factor_id": "n-shape",
            "scope": {"type": "symbols", "symbols": ["000001.SZ"]},
            "parameters": {"start": "2026-01-01", "end": "2026-06-01"},
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "preflight_blocked"
    assert FactorJobStore(tmp_path).list_runs() == []


def test_ready_interactive_run_returns_accepted_envelope(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        runs_api,
        "plan_factor_run",
        lambda *_args, **_kwargs: (definition, scope, checked, parameters),
    )
    monkeypatch.setattr(runs_api.InteractiveWorker, "submit", lambda *_args, **_kwargs: None)

    response = _client(tmp_path).post(
        "/api/research/runs",
        json={
            "factor_id": "n-shape",
            "scope": scope.model_dump(mode="json"),
            "parameters": parameters.model_dump(mode="json"),
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_status"] == "pending"
    assert body["links"]["stream"].endswith("/stream")
    assert FactorJobStore(tmp_path).get(body["run_id"])["data_status"] == "ready"


def _client(tmp_path):
    app = FastAPI()
    app.state.repo = SimpleNamespace(data_dir=tmp_path, store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(governance_router)
    app.include_router(router)
    install_error_handlers(app)
    return TestClient(app)


def test_factor_run_evidence_link_and_reverse_lookup(tmp_path):
    client = _client(tmp_path)
    run_id = new_run_id()
    FactorJobStore(tmp_path).create(
        {
            "run_id": run_id,
            "factor_id": "n-shape",
            "profile": "event_signal",
            "scope": {"type": "symbols", "symbols": ["000001.SZ"]},
            "parameters": {"start": "2024-01-01", "end": "2024-12-31"},
        }
    )
    hypothesis = ResearchStore(tmp_path).create_hypothesis("题目", "假设")
    response = client.post(
        f"/api/research/runs/{run_id}/links",
        json={"hypothesis_id": hypothesis.id, "summary": "统一因子证据"},
    )
    assert response.status_code == 201
    assert response.json()["hypothesis"]["evidence"][-1]["kind"] == "factor_run"
    detail = client.get(f"/api/research/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["hypotheses"][0]["id"] == hypothesis.id
    assert (
        client.post(
            f"/api/research/runs/{run_id}/links",
            json={"hypothesis_id": hypothesis.id},
        ).status_code
        == 201
    )


def test_factor_run_evidence_link_validates_both_sides(tmp_path):
    client = _client(tmp_path)
    hypothesis = ResearchStore(tmp_path).create_hypothesis("题目", "假设")
    assert (
        client.post(
            "/api/research/runs/rr-0000000000000000/links",
            json={"hypothesis_id": hypothesis.id},
        ).status_code
        == 404
    )


def _job(
    tmp_path,
    *,
    factor_id: str = "n-shape",
    created_at: str = "2026-08-31T10:00:00+00:00",
    verdict: str = "inconclusive",
    data_status: str = "missing",
):
    return FactorJobStore(tmp_path).create(
        {
            "run_id": new_run_id(),
            "factor_id": factor_id,
            "profile": "event_signal",
            "scope": {"type": "symbols", "symbols": ["000001.SZ"]},
            "parameters": {"start": "2024-01-01", "end": "2024-12-31"},
            "created_at": created_at,
            "verdict": verdict,
            "data_status": data_status,
        }
    )


def test_catalog_enriches_from_latest_run_and_filters(tmp_path):
    older = _job(
        tmp_path,
        created_at="2026-08-31T09:00:00+00:00",
        verdict="rejected",
        data_status="ready",
    )
    latest = _job(
        tmp_path,
        created_at="2026-08-31T10:00:00+00:00",
        verdict="unavailable",
        data_status="missing",
    )
    client = _client(tmp_path)

    body = client.get("/api/research/factors?verdict=unavailable&data_status=missing").json()
    assert [item["id"] for item in body["items"]] == ["n-shape"]
    assert body["items"][0]["latest_run_id"] == latest["run_id"]
    assert body["items"][0]["latest_run_id"] != older["run_id"]


def test_run_list_is_newest_first_with_descending_cursor(tmp_path):
    older = _job(tmp_path, created_at="2026-08-31T09:00:00+00:00")
    latest = _job(tmp_path, created_at="2026-08-31T10:00:00+00:00")
    client = _client(tmp_path)

    first = client.get("/api/research/runs?limit=1").json()
    assert first["items"][0]["run_id"] == latest["run_id"]
    second = client.get(
        "/api/research/runs", params={"limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert second["items"][0]["run_id"] == older["run_id"]


def test_patch_returns_complete_detail_envelope(tmp_path):
    run = _job(tmp_path)
    response = _client(tmp_path).patch(
        f"/api/research/runs/{run['run_id']}", json={"favorite": True, "label": "复核"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["favorite"] is True
    assert body["label"] == "复核"
    assert body["links"]["events"].endswith("/events")
    assert body["hypotheses"] == []
    assert "result" in body


def test_event_date_filter_and_output_contract(tmp_path):
    run = _job(tmp_path)
    FactorRunStore(tmp_path).publish(
        run["run_id"],
        {"status": "ready"},
        {"status": "ready"},
        [
            {"symbol": "000001.SZ", "event_date": "2026-08-28"},
            {"symbol": "000001.SZ", "event_date": "2026-08-29"},
        ],
        [],
    )
    response = _client(tmp_path).get(
        f"/api/research/runs/{run['run_id']}/events", params={"date": "2026-08-29"}
    )
    assert response.status_code == 200
    assert [row["event_date"] for row in response.json()["items"]] == ["2026-08-29"]


def test_invalid_run_and_sse_cursor_use_structured_errors(tmp_path):
    client = _client(tmp_path)
    invalid = client.get("/api/research/runs/not-a-run")
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_run_id"

    run = _job(tmp_path)
    cursor = client.get(
        f"/api/research/runs/{run['run_id']}/stream",
        headers={"Last-Event-ID": "-1"},
    )
    assert cursor.status_code == 400
    assert cursor.json()["error"]["code"] == "invalid_last_event_id"
    query_cursor = client.get(
        f"/api/research/runs/{run['run_id']}/stream",
        params={"last_event_id": -1},
    )
    assert query_cursor.status_code == 400
    assert query_cursor.json()["error"]["code"] == "invalid_last_event_id"
