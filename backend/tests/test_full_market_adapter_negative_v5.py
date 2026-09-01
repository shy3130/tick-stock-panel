from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services.full_market_adapters.negative_v5 import (
    UNIVERSE_READER_ATTR,
    NegativeV5Adapter,
)
from app.services.full_market_research import RunnerContext
from app.services.negative_exclusion import (
    CAPABILITY_V1_UNVERIFIED,
    CAPABILITY_V3_NO_PIT_SOURCE,
    CLASS_V5,
    capability_report,
)

START = date(2024, 1, 1)
OOS_START = date(2024, 7, 1)
END = date(2025, 1, 31)
COHORT = ["600000.SH", "000001.SZ", "300750.SZ", "688001.SH"]


def _request():
    return NegativeV5Adapter().build_request(START, END, COHORT, oos_start=OOS_START, cost_bps=12.5)


def test_build_request_keeps_complete_cohort_and_v5_defaults():
    request = NegativeV5Adapter().build_request(
        START, END, COHORT, oos_start=OOS_START, cost_bps=None
    )

    assert request.symbols == COHORT
    assert request.symbols is not COHORT
    assert request.start == START
    assert request.end == END
    assert request.oos_start == OOS_START
    assert request.cost_bps == 20.0
    assert request.horizon_days == 10


def test_evaluate_passes_full_cohort_once_and_only_enables_v5(monkeypatch):
    adapter = NegativeV5Adapter()
    request = _request()
    calls = []
    expected = {"status": "ok"}

    def fake_evaluator(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        "app.services.full_market_adapters.negative_v5.evaluate_negative_exclusion_production",
        fake_evaluator,
    )
    universe = object()
    result = adapter.evaluate(
        RunnerContext(repo=SimpleNamespace(**{UNIVERSE_READER_ATTR: universe}), reader=object()),
        request,
    )

    assert result is expected
    assert len(calls) == 1
    assert tuple(calls[0]["symbols"]) == tuple(COHORT)
    assert calls[0]["enabled_classes"] == (CLASS_V5,)
    assert calls[0]["canonical_reader"] is calls[0]["market_facts_reader"]
    assert calls[0]["universe_reader"] is universe


def test_evaluate_preloads_full_cohort_panel_once(monkeypatch):
    adapter = NegativeV5Adapter()
    request = _request()
    calls = []
    evaluator_calls = []

    class Reader:
        def preload_panel(self, start, end, *, symbols):
            calls.append((start, end, list(symbols)))

    def fake_evaluator(**kwargs):
        evaluator_calls.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(
        "app.services.full_market_adapters.negative_v5.evaluate_negative_exclusion_production",
        fake_evaluator,
    )
    adapter.evaluate(
        RunnerContext(
            repo=SimpleNamespace(**{UNIVERSE_READER_ATTR: object()}),
            reader=Reader(),
        ),
        request,
    )

    assert calls == [
        (
            START - timedelta(days=600),
            END + timedelta(days=180),
            COHORT,
        )
    ]
    assert len(evaluator_calls) == 1


def test_v1_v3_remain_explicitly_blocked_and_v5_is_symmetric():
    capabilities = capability_report()
    assert capabilities["v1"] == CAPABILITY_V1_UNVERIFIED
    assert capabilities["v3"] == CAPABILITY_V3_NO_PIT_SOURCE

    verdict = {
        "coverage": {"observations": len(COHORT)},
        "evaluation": {
            "classes": {
                "v1": {
                    "capability": CAPABILITY_V1_UNVERIFIED,
                    "verdict": "unavailable_capability",
                },
                "v3": {
                    "capability": CAPABILITY_V3_NO_PIT_SOURCE,
                    "verdict": "unavailable_capability",
                },
                "v5": {
                    "capability": "available",
                    "missed_rebounds": {"count": 3, "sum_return": 0.12},
                    "avoided_declines": {"count": 4, "sum_return": -0.25},
                    "net_benefit": 0.13,
                    "portfolio": {"return_delta": 0.04, "drawdown_delta": -0.08},
                },
            }
        },
    }
    classes = verdict["evaluation"]["classes"]
    assert classes["v1"]["verdict"] == "unavailable_capability"
    assert classes["v3"]["verdict"] == "unavailable_capability"
    v5 = classes["v5"]
    assert set(("missed_rebounds", "avoided_declines")) <= set(v5)
    assert v5["net_benefit"] == pytest.approx(
        -v5["avoided_declines"]["sum_return"] - v5["missed_rebounds"]["sum_return"]
    )
    assert v5["portfolio"]["return_delta"] is not None


def test_missing_pit_universe_is_unavailable_without_fallback():
    adapter = NegativeV5Adapter()
    result = adapter.evaluate(RunnerContext(repo=SimpleNamespace(), reader=object()), _request())
    serialized = adapter.serialize_verdict(result)

    assert serialized["status"] == "unavailable"
    assert serialized["reason"] == "unavailable_universe_presence_reader"
    assert "evaluation" not in serialized
    assert serialized["promoted"] is False
    assert adapter.extract_coverage(serialized) is None


def test_serialization_and_coverage_are_json_safe():
    adapter = NegativeV5Adapter()
    verdict = {"request": {"start": START}, "coverage": {"observations": 4}}

    serialized = adapter.serialize_verdict(verdict)
    assert serialized["request"]["start"] == START.isoformat()
    assert adapter.extract_coverage(serialized) == {"observations": 4}


def test_build_request_consumes_negative_parameters():
    request = NegativeV5Adapter().build_request(
        START,
        END,
        COHORT,
        oos_start=OOS_START,
        cost_bps=None,
        parameters={
            "start": START,
            "oos_start": OOS_START,
            "end": END,
            "enabled_classes": ["v5"],
            "horizon_days": 15,
            "cost_bps": 25.0,
        },
    )
    assert request.enabled_classes == ("v5",)
    assert request.horizon_days == 15
