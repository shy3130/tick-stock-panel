from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.research import router


def _client(repo=None) -> TestClient:
    app = FastAPI()
    app.state.repo = repo or SimpleNamespace()
    app.include_router(router)
    return TestClient(app)


def _daily_body() -> dict[str, object]:
    return {
        "symbols": ["000001.SZ"],
        "start": "2025-01-02",
        "oos_start": "2025-07-01",
        "end": "2025-12-31",
    }


def test_new_research_routes_fail_closed_without_pinned_sources():
    client = _client()

    doji = client.post("/api/research/factors/doji-patterns/evaluate", json=_daily_body())
    chip = client.post("/api/research/factors/chip-peak-patterns/evaluate", json=_daily_body())
    weekly = client.post(
        "/api/research/factors/weekly-flagpole/evaluate",
        json={"start": "2025-01-02", "end": "2025-12-31"},
    )
    calendar = client.post("/api/research/escape-windows/evaluate", json={})

    for response in (doji, chip, weekly, calendar):
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "unavailable"

    assert doji.json()["unavailable_reason"] == "unavailable_canonical_reader"
    assert chip.json()["unavailable_reason"] == "unavailable_canonical_reader"
    assert weekly.json()["unavailable_reasons"]
    assert calendar.json()["unavailable_reasons"]


def test_new_research_request_models_forbid_unknown_fields():
    client = _client()
    cases = (
        ("/api/research/factors/doji-patterns/evaluate", _daily_body()),
        ("/api/research/factors/chip-peak-patterns/evaluate", _daily_body()),
        (
            "/api/research/factors/weekly-flagpole/evaluate",
            {"start": "2025-01-02", "end": "2025-12-31"},
        ),
        ("/api/research/escape-windows/evaluate", {}),
    )

    for route, body in cases:
        response = client.post(route, json={**body, "unexpected": True})
        assert response.status_code == 422, (route, response.text)


def test_new_capability_routes_are_registered():
    client = _client()

    for route in (
        "/api/research/doji-patterns",
        "/api/research/weekly-flagpole",
        "/api/research/escape-windows",
    ):
        response = client.get(route)
        assert response.status_code == 200, (route, response.text)

    escape = client.get("/api/research/escape-windows")
    assert "readers" not in escape.json()


class _ClosableIncompleteReader:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_weekly_capability_closes_incomplete_reader():
    reader = _ClosableIncompleteReader()
    repo = SimpleNamespace(n_shape_research_reader=reader)

    response = _client(repo).get("/api/research/weekly-flagpole")

    assert response.status_code == 200
    assert response.json()["methods_complete"] is False
    assert reader.closed is True
