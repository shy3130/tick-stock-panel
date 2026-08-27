from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import research as research_api


_ROUTE = "/api/research/factors/volume-breakout/evaluate"


def _client() -> TestClient:
    app = FastAPI()
    app.state.repo = SimpleNamespace()
    app.include_router(research_api.router)
    return TestClient(app)


def test_missing_reader_returns_structured_unavailable():
    response = _client().post(
        _ROUTE,
        json={"start": "2026-01-01", "end": "2026-01-31", "symbols": ["600000.SH"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["capabilities"] == {
        "generation_pinned_reader": False,
        "pit_eligible_universe": False,
        "versioned_exchange_calendar": False,
    }
    assert body["events"] == []
    assert not any("not_implemented" in reason for reason in body["unavailable_reasons"])


def test_request_schema_forbids_unknown_fields():
    response = _client().post(
        _ROUTE,
        json={"start": "2026-01-01", "end": "2026-01-31", "unexpected": True},
    )

    assert response.status_code == 422


def test_request_schema_requires_dates():
    response = _client().post(_ROUTE, json={"end": "2026-01-31"})

    assert response.status_code == 422


def test_request_schema_limits_symbols():
    response = _client().post(
        _ROUTE,
        json={
            "start": "2026-01-01",
            "end": "2026-01-31",
            "symbols": ["600000.SH"] * 1001,
        },
    )

    assert response.status_code == 422


def test_invalid_range_maps_to_bad_request():
    response = _client().post(
        _ROUTE,
        json={"start": "2026-02-01", "end": "2026-01-01"},
    )

    assert response.status_code == 400
