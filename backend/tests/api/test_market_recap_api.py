from datetime import date
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import market_recap


def _client() -> TestClient:
    repo = SimpleNamespace(
        get_enriched_latest=lambda: (pl.DataFrame(), date(2026, 8, 10)),
    )
    app = FastAPI()
    app.state.repo = repo
    app.include_router(market_recap.router)
    return TestClient(app)


def test_report_list_isolates_reports_beyond_canonical_date(monkeypatch):
    monkeypatch.setattr(
        market_recap.market_recap_reports,
        "list_reports",
        lambda: [
            {"id": "valid", "as_of": "2026-08-10", "content": "ok"},
            {"id": "future", "as_of": "2026-08-11", "content": "bad"},
        ],
    )

    payload = _client().get("/api/market-recap/reports").json()

    assert payload == {
        "reports": [
            {"id": "valid", "as_of": "2026-08-10", "content": "ok"}
        ],
        "canonical_as_of": "2026-08-10",
        "discarded_reports": [
            {"id": "future", "as_of": "2026-08-11"}
        ],
    }


def test_future_report_cannot_be_analyzed_or_saved(monkeypatch):
    saved: list[dict] = []
    monkeypatch.setattr(
        market_recap.market_recap_reports,
        "save_report",
        lambda value: saved.append(value),
    )
    client = _client()

    analyze = client.post(
        "/api/market-recap/analyze",
        json={"as_of": "2026-08-11"},
    )
    save = client.post(
        "/api/market-recap/reports",
        json={"as_of": "2026-08-11", "content": "bad"},
    )

    assert analyze.status_code == 409
    assert analyze.json()["detail"]["code"] == "unconfirmed_as_of"
    assert save.status_code == 409
    assert save.json()["detail"]["code"] == "unconfirmed_as_of"
    assert saved == []
