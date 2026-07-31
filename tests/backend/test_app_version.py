from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router


def test_health_exposes_uncached_build_metadata(monkeypatch) -> None:
    monkeypatch.setenv("BUILD_ID", "20260731-deadbee")
    monkeypatch.setenv("PUBLISHED_AT", "2026-07-31T10:00:00+08:00")
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["build_id"] == "20260731-deadbee"
    assert response.json()["published_at"] == "2026-07-31T10:00:00+08:00"
