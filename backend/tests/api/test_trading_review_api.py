from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trading_review


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(trading_review.router)
    return TestClient(app)


def test_list_proposals_uses_proposals_service(monkeypatch):
    calls = []

    def fake_list_proposals(data_dir, status):
        calls.append((data_dir, status))
        return [{"id": "prop_20260826_001", "status": status}]

    monkeypatch.setattr(trading_review.proposals_svc, "list_proposals", fake_list_proposals)

    response = _client().get("/api/trading/proposals?status=trial")

    assert response.status_code == 200
    assert response.json() == {
        "proposals": [{"id": "prop_20260826_001", "status": "trial"}]
    }
    assert calls == [(trading_review.settings.data_dir, "trial")]
