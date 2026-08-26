from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import market_state as market_state_api
from app.services.market_concentration import (
    MarketStateCoverage,
    MarketStateDataError,
    MarketStateGates,
    MarketStateMetrics,
    MarketStatePercentiles,
    MarketStateSnapshot,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir="/tmp/not-read"))
    app.include_router(market_state_api.router)
    return app


def _snapshot(state: str = "transition") -> MarketStateSnapshot:
    return MarketStateSnapshot(
        available=True,
        state=state,
        target_date="2026-08-25",
        signal_date="2026-08-24",
        metrics=MarketStateMetrics(
            return_std=0.02,
            return_q90_q10=0.05,
            turnover_hhi=0.2,
            positive_return_hhi=0.3,
            top3_contribution=0.4,
            top5_contribution=0.6,
        ),
        percentiles=MarketStatePercentiles(
            return_std=0.6,
            turnover_hhi=0.5,
            positive_return_hhi=0.5,
            top3_contribution=0.5,
        ),
        coverage=MarketStateCoverage(
            stock_count=5000,
            industry_count=31,
            symbol_coverage=0.99,
            amount_symbol_coverage=0.99,
            turnover_coverage=0.99,
            calibration_days=180,
        ),
        gates=MarketStateGates(
            automatic_research_allowed=state == "dispersed",
            reasons=[] if state == "dispersed" else [f"market_state_not_dispersed:{state}"],
        ),
    )


def test_market_state_api_returns_unwrapped_exact_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        market_state_api, "market_state_for_date", lambda _repo, _as_of: _snapshot()
    )

    response = TestClient(_app()).get("/api/research/t-suitability/market-state?as_of=2026-08-25")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "available",
        "state",
        "target_date",
        "signal_date",
        "methodology",
        "metrics",
        "percentiles",
        "coverage",
        "gates",
        "reason",
        "warnings",
        "source",
    }
    assert body["state"] == "transition"
    assert body["methodology"]["hidden_formula_replicated"] is False
    assert body["source"]["external_fallback"] is False


def test_market_state_api_preserves_200_unavailable(monkeypatch) -> None:
    unavailable = MarketStateSnapshot(
        available=False,
        state="unavailable",
        target_date="2026-08-25",
        signal_date="2026-08-24",
        gates=MarketStateGates(
            automatic_research_allowed=False,
            reasons=["market_state_unavailable:insufficient_calibration"],
        ),
        reason="insufficient_calibration",
    )
    monkeypatch.setattr(
        market_state_api, "market_state_for_date", lambda _repo, _as_of: unavailable
    )

    response = TestClient(_app()).get("/api/research/t-suitability/market-state")

    assert response.status_code == 200
    assert response.json()["state"] == "unavailable"
    assert response.json()["gates"]["automatic_research_allowed"] is False


def test_market_state_api_sanitizes_read_failure(monkeypatch) -> None:
    def fail(_repo, _as_of):
        raise MarketStateDataError("/Volumes/private/financials/part.parquet")

    monkeypatch.setattr(market_state_api, "market_state_for_date", fail)
    response = TestClient(_app()).get("/api/research/t-suitability/market-state")

    assert response.status_code == 503
    assert response.json() == {"detail": "market state data unavailable"}
    assert "/Volumes" not in response.text


def test_market_state_api_rejects_invalid_date() -> None:
    response = TestClient(_app()).get("/api/research/t-suitability/market-state?as_of=2026-02-31")

    assert response.status_code == 422
