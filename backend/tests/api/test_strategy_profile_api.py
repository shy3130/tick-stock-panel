from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy_profile


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(strategy_profile.router)
    return TestClient(app)


def test_get_missing_profile_returns_404_instead_of_500(monkeypatch):
    monkeypatch.setattr(strategy_profile, "read_profile", lambda *_args: None)

    response = _client().get("/api/strategies/boll_breakout/profile")

    assert response.status_code == 404
    assert "未声明风险 profile" in response.json()["detail"]


def test_validate_profile_endpoint_runs_mechanical_checks(monkeypatch):
    monkeypatch.setattr(strategy_profile, "read_profile", lambda *_args: None)
    monkeypatch.setattr(strategy_profile.journal_store, "read_ledger", lambda *_args: None)
    monkeypatch.setattr(strategy_profile.proposals_svc, "list_proposals", lambda *_args: [])

    response = _client().get("/api/strategies/boll_breakout/profile/validate")

    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks
    assert {check["status"] for check in checks} <= {
        "pass",
        "partial",
        "fail",
        "insufficient_evidence",
    }
