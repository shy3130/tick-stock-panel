from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.research import router as research_router
from app.services.macd_stages import (
    MACD_PARAMS,
    STATE_VALUES,
    classify_stage,
    evaluate_macd_stages,
    macd_stages_availability,
)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(research_router)
    return TestClient(app)


class _Reader:
    def __init__(self, closes: list[float]):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes) + 1)]
        self.frame = pl.DataFrame({
            "symbol": ["600000.SH"] * len(closes),
            "date": self.days[:len(closes)],
            "raw_close": closes,
        })

    def generation(self):
        return "generation-test"

    def manifest_sha256(self):
        return "a" * 64

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def daily_bars(self, symbol, start, end):
        return self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def test_capability_endpoint_reports_only_real_reader_gap():
    body = _client().get("/api/research/macd-stages").json()
    assert body["status"] == "unavailable"
    assert body["reasons"] == ["generation_pinned_reader_missing"]
    assert body["missing_capabilities"] == {
        "daily_state_machine": False,
        "oos_evaluation": False,
        "pit_reader": True,
    }
    assert "rows" not in body


def test_parameters_and_stage_classifier_are_frozen():
    assert MACD_PARAMS == {"fast": 10, "slow": 20, "signal": 7}
    assert set(STATE_VALUES) == {
        "initial", "below_shrink", "below_expand", "cross_up",
        "above_expand", "above_shrink", "cross_down",
    }
    assert classify_stage((-1.0, -0.5, -0.5), (-0.4, -0.5, 0.1)) == "cross_up"
    assert classify_stage((0.5, 0.4, 0.1), (0.3, 0.4, -0.1)) == "cross_down"
    assert classify_stage((-1.0, -0.5, -0.5), (-0.8, -0.5, -0.3)) == "below_shrink"
    assert classify_stage((1.0, 0.5, 0.5), (1.2, 0.5, 0.7)) == "above_expand"


def test_evaluate_emits_t_plus_one_and_separate_oos_segments():
    reader = _Reader([10.0 + index * 0.1 for index in range(70)])
    result = evaluate_macd_stages(
        reader,
        start=date(2026, 1, 1),
        end=date(2026, 3, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 2, 10),
    )

    assert result["status"] == "ok"
    assert result["provenance"]["pinned_reader"] == {
        "generation": "generation-test",
        "manifest_sha256": "a" * 64,
    }
    is_rows = result["segments"]["is"]["rows"]
    oos_rows = result["segments"]["oos"]["rows"]
    assert is_rows and oos_rows
    assert all(row["available_from"] > row["market_date"] for row in is_rows + oos_rows)
    assert all(row["pit"]["generation"] == row["generation"] == "generation-test" for row in is_rows + oos_rows)
    assert max(row["market_date"] for row in is_rows) < date(2026, 2, 10)
    assert min(row["market_date"] for row in oos_rows) >= date(2026, 2, 10)


def test_constant_series_keeps_equal_state_unclassified_after_initial():
    result = evaluate_macd_stages(
        _Reader([10.0] * 50),
        start=date(2026, 1, 1), end=date(2026, 2, 18),
        symbols=["600000.SH"], oos_start=date(2026, 2, 1),
    )
    rows = result["segments"]["is"]["rows"] + result["segments"]["oos"]["rows"]
    assert rows[0]["state"] == "initial"
    assert all(row["zero_side"] == "zero" for row in rows)
    assert all(row["state"] is None for row in rows[1:])


def test_missing_reader_fails_closed_without_old_not_implemented_reasons():
    availability = macd_stages_availability().as_dict()
    assert availability["status"] == "unavailable"
    assert not any("not_implemented" in reason for reason in availability["reasons"])
