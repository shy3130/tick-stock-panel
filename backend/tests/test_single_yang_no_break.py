"""单阳不破定义与 fail-closed API 契约测试。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.research import router as research_router
from app.services.single_yang_no_break import (
    Bar,
    OOS_NOT_IMPLEMENTED,
    PIT_READER_MISSING,
    STATE_MACHINE_NOT_IMPLEMENTED,
    assess_capability,
    detect_single_yang,
    is_single_yang,
    run_single_yang_research,
)


def _bars(*lows: float) -> list[Bar]:
    return [Bar(open=100, high=106, low=low, close=105) for low in lows]


def test_single_yang_requires_raw_body_threshold_and_rejects_doji():
    assert is_single_yang(Bar(100, 106, 99, 103))
    assert not is_single_yang(Bar(100, 106, 99, 101))
    assert not is_single_yang(Bar(100, 106, 99, 100))



def test_exact_two_percent_body_preserves_decimal_boundary():
    assert (5.10 - 5.00) / 5.00 < 0.02
    assert is_single_yang(Bar(open=5.00, high=5.12, low=4.99, close=5.10))
    assert is_single_yang(Bar(open=2.50, high=2.56, low=2.49, close=2.55))
    assert not is_single_yang(Bar(open=5.00, high=5.09, low=4.99, close=5.09))


def test_exact_boundary_anchor_confirms_after_complete_window():
    anchor = Bar(open=5.00, high=5.12, low=4.99, close=5.10)
    follow_up = [Bar(open=5.05, high=5.20, low=4.99, close=5.08) for _ in range(5)]

    assert detect_single_yang([anchor, *follow_up]) == [0]

def test_detect_requires_complete_window_and_equal_low_is_not_break():
    assert detect_single_yang(_bars(99, 99, 100, 99, 99, 99)) == [0]
    assert detect_single_yang(_bars(99, 99, 98, 99, 99, 99)) == []
    assert detect_single_yang(_bars(99, 99, 99)) == []


def test_capability_is_unavailable_for_all_required_gates():
    capability = assess_capability()
    assert capability["available"] is False
    assert set(capability["reasons"]) == {
        PIT_READER_MISSING,
        STATE_MACHINE_NOT_IMPLEMENTED,
        OOS_NOT_IMPLEMENTED,
    }


def test_research_stays_unavailable_even_with_bars():
    result = run_single_yang_research(bars=_bars(99, 99, 99, 99, 99, 99))
    assert result["status"] == "unavailable"
    assert set(result["reasons"]) == {
        PIT_READER_MISSING,
        STATE_MACHINE_NOT_IMPLEMENTED,
        OOS_NOT_IMPLEMENTED,
    }
    assert not any(key in result for key in ("signals", "orders", "positions", "stop_loss"))


def test_research_endpoint_returns_fail_closed_contract():
    app = FastAPI()
    app.include_router(research_router)
    response = TestClient(app).get("/api/research/single-yang-no-break")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["definition"]["price_basis"] == "raw_unadjusted"
    assert body["definition"]["window"] == 5
    assert body["definition"]["signal_timing"] == "T_plus_5_close_confirmed; evaluation_starts_T_plus_6"
    assert not any(key in body for key in ("order", "position", "trade", "entry", "exit"))
