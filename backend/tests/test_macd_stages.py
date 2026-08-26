from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.research import router as research_router
from app.services.macd_stages import MACD_PARAMS, macd_stages_availability


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(research_router)
    return TestClient(app)


def test_endpoint_returns_fixed_unavailable_contract():
    response = _client().get("/api/research/macd-stages")

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "tickflow.research.macd-stages.v1"
    assert body["status"] == "unavailable"
    assert body["params"] == {"fast": 10, "slow": 20, "signal": 7}
    assert set(body["reasons"]) == {
        "state_machine_not_implemented",
        "oos_not_implemented",
    }


def test_endpoint_has_no_fabricated_stage_rows():
    body = _client().get("/api/research/macd-stages").json()

    assert "rows" not in body
    assert "series" not in body
    assert body["missing_capabilities"] == {
        "daily_state_machine": True,
        "oos_evaluation": True,
        "pit_reader": True,
    }


def test_service_is_deterministic_and_parameters_are_frozen():
    first = macd_stages_availability().as_dict()
    second = macd_stages_availability().as_dict()

    assert first == second
    assert MACD_PARAMS == {"fast": 10, "slow": 20, "signal": 7}
    assert first["status"] == "unavailable"
