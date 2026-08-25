from datetime import date
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import regime
from app.services import regime_builder


def _client(tmp_path) -> TestClient:
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest=lambda: (pl.DataFrame(), date(2026, 8, 10)),
    )
    app = FastAPI()
    app.state.repo = repo
    app.include_router(regime.router)
    return TestClient(app)


def _write_history(tmp_path) -> None:
    regime_builder.upsert_regime_history(
        tmp_path,
        pl.DataFrame(
            {
                "date": [date(2026, 8, 10), date(2026, 8, 11)],
                "state": ["strong", "weak"],
                "score": [80, 20],
            }
        ),
    )
    regime.invalidate_regime_cache()


def test_regime_reads_hide_rows_beyond_canonical_date(tmp_path):
    _write_history(tmp_path)
    client = _client(tmp_path)

    coverage = client.get("/api/regime/coverage").json()
    history = client.get("/api/regime/history").json()
    latest = client.get("/api/regime/latest").json()
    states = client.get("/api/regime/states").json()

    assert coverage == {
        "rows": 1,
        "earliest_date": "2026-08-10",
        "latest_date": "2026-08-10",
    }
    assert [row["date"] for row in history["rows"]] == ["2026-08-10"]
    assert latest["row"]["date"] == "2026-08-10"
    assert states == {
        "distribution": [
            {"state": "strong", "label": "强势", "count": 1, "pct": 100.0}
        ],
        "days": 1,
    }


def test_regime_recompute_clamps_requested_end_to_canonical(tmp_path, monkeypatch):
    captured: dict[str, date] = {}

    def run_batch(repo, start, end):
        captured["start"] = start
        captured["end"] = end
        return pl.DataFrame()

    monkeypatch.setattr(regime.regime_builder, "run_regime_batch", run_batch)
    client = _client(tmp_path)

    response = client.post(
        "/api/regime/recompute?start=2026-08-10&end=2026-08-11"
    )

    assert response.status_code == 200
    assert captured == {
        "start": date(2026, 8, 10),
        "end": date(2026, 8, 10),
    }
