import json
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from app.services.daily_event_research.pre_surge import PreSurgeVerdict
from app.services.full_market_adapters import pre_surge as pre_surge_module
from app.services.full_market_adapters.pre_surge import (
    BENCHMARK_SYMBOL,
    PreSurgeAdapter,
)
from app.services.full_market_research import RunnerContext
from app.services.hold_firm_patterns.adapters import ProductionReaderScopeUnavailable
from app.services.hold_firm_patterns.models import UnavailabilityReason

START = date(2024, 1, 1)
OOS_START = date(2025, 1, 1)
END = date(2025, 1, 31)


@pytest.fixture(autouse=True)
def _matching_pinned_sources(monkeypatch):
    monkeypatch.setattr(
        pre_surge_module,
        "production_scope_matches",
        lambda context, canonical, facts: True,
    )


def test_build_request_keeps_complete_cohort_and_requires_frozen_oos_boundary():
    adapter = PreSurgeAdapter()
    cohort = ["600000.SH", "000001.SZ", "300750.SZ"]

    request = adapter.build_request(
        START,
        END,
        cohort,
        oos_start=OOS_START,
        cost_bps=12.5,
    )

    assert request.symbols == cohort
    assert request.symbols is not cohort
    assert request.oos_start == OOS_START
    assert request.cost_bps == 12.5
    assert request.benchmark_symbol == BENCHMARK_SYMBOL

    with pytest.raises(ValueError, match="explicit frozen oos_start"):
        adapter.build_request(START, END, cohort, oos_start=None, cost_bps=None)


def test_evaluate_invokes_production_evaluator_once_with_full_cohort(monkeypatch):
    adapter = PreSurgeAdapter()
    cohort = ["600000.SH", "000001.SZ", "300750.SZ"]
    request = adapter.build_request(
        START,
        END,
        cohort,
        oos_start=OOS_START,
        cost_bps=12.5,
    )
    preload_calls = []
    events = []

    def preload_panel(preload_start, preload_end, *, symbols):
        events.append("preload")
        preload_calls.append((preload_start, preload_end, symbols))

    canonical = SimpleNamespace(preload_panel=preload_panel)
    market_facts = object()
    universe = object()
    calls = []
    expected = {"schema": "daily_event_research/pre_surge/v1", "status": "ok"}

    @contextmanager
    def fake_scope(repo):
        assert repo is sentinel_repo
        yield SimpleNamespace(
            canonical=canonical,
            market_facts=market_facts,
            universe_reader=universe,
        )

    def fake_evaluate(**kwargs):
        events.append("evaluate")
        calls.append(kwargs)
        return expected

    sentinel_repo = object()
    monkeypatch.setattr(
        "app.services.full_market_adapters.pre_surge.production_reader_scope",
        fake_scope,
    )
    monkeypatch.setattr(
        "app.services.full_market_adapters.pre_surge.evaluate_pre_surge_production",
        fake_evaluate,
    )

    result = adapter.evaluate(RunnerContext(repo=sentinel_repo, reader=object()), request)

    assert result is expected
    assert events == ["preload", "evaluate"]
    assert preload_calls == [
        (
            date(2022, 11, 27),
            date(2025, 5, 31),
            cohort,
        )
    ]
    assert len(calls) == 1
    assert calls[0] == {
        "symbols": cohort,
        "start": START,
        "oos_start": OOS_START,
        "end": END,
        "canonical_reader": canonical,
        "market_facts_reader": market_facts,
        "universe_reader": universe,
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "cost_bps": 12.5,
    }


def test_evaluate_allows_canonical_without_optional_preload(monkeypatch):
    adapter = PreSurgeAdapter()
    request = adapter.build_request(START, END, ["600000.SH"], oos_start=OOS_START, cost_bps=None)
    expected = {"status": "ok"}
    calls = []

    @contextmanager
    def fake_scope(repo):
        yield SimpleNamespace(
            canonical=SimpleNamespace(), market_facts=object(), universe_reader=object()
        )

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        "app.services.full_market_adapters.pre_surge.production_reader_scope", fake_scope
    )
    monkeypatch.setattr(
        "app.services.full_market_adapters.pre_surge.evaluate_pre_surge_production", fake_evaluate
    )

    assert adapter.evaluate(RunnerContext(repo=object(), reader=object()), request) is expected
    assert len(calls) == 1


def test_preload_failure_returns_unavailable_without_evaluator_fallback(monkeypatch):
    adapter = PreSurgeAdapter()
    request = adapter.build_request(START, END, ["600000.SH"], oos_start=OOS_START, cost_bps=None)

    def preload_panel(*args, **kwargs):
        raise OSError("panel read failed")

    @contextmanager
    def fake_scope(repo):
        yield SimpleNamespace(
            canonical=SimpleNamespace(preload_panel=preload_panel),
            market_facts=object(),
            universe_reader=object(),
        )

    def unexpected_evaluate(**kwargs):
        raise AssertionError("production evaluator must not run after preload failure")

    monkeypatch.setattr(
        "app.services.full_market_adapters.pre_surge.production_reader_scope", fake_scope
    )
    monkeypatch.setattr(
        "app.services.full_market_adapters.pre_surge.evaluate_pre_surge_production",
        unexpected_evaluate,
    )

    assert adapter.evaluate(RunnerContext(repo=object(), reader=object()), request) == {
        "schema": "daily_event_research/pre_surge/v1",
        "status": "unavailable",
        "reason": "unavailable_preload_panel_failed",
        "detail": "panel read failed",
        "promoted": False,
    }


def test_missing_reader_scope_returns_explicit_unavailable_without_fallback(monkeypatch):
    adapter = PreSurgeAdapter()
    request = adapter.build_request(
        START,
        END,
        ["600000.SH"],
        oos_start=OOS_START,
        cost_bps=None,
    )

    @contextmanager
    def missing_scope(repo):
        raise ProductionReaderScopeUnavailable(
            UnavailabilityReason.CANONICAL_READER,
            "canonical history is not published",
        )
        yield  # pragma: no cover

    def unexpected_evaluate(**kwargs):  # pragma: no cover
        raise AssertionError("production evaluator must not run without reader scope")

    monkeypatch.setattr(
        "app.services.full_market_adapters.pre_surge.production_reader_scope",
        missing_scope,
    )
    monkeypatch.setattr(
        "app.services.full_market_adapters.pre_surge.evaluate_pre_surge_production",
        unexpected_evaluate,
    )

    result = adapter.evaluate(RunnerContext(repo=object(), reader=object()), request)

    assert result == {
        "schema": "daily_event_research/pre_surge/v1",
        "status": "unavailable",
        "reason": "unavailable_canonical_reader",
        "detail": "canonical history is not published",
        "promoted": False,
    }


def test_serialize_verdict_preserves_risk_metrics_and_definitions():
    adapter = PreSurgeAdapter()
    coverage = {"symbols": 3, "evaluated_detections": 9}
    risk_metrics = {
        "f1_limit_up": {"events": 2, "mean_return": 0.125, "max_drawdown": None},
        "f2_gap_unfilled": None,
    }
    definitions = {
        "mean_return": "Arithmetic mean of reachable net returns",
        "max_drawdown": "Peak-to-trough drawdown",
    }
    verdict = {
        "status": "ok",
        "coverage": coverage,
        "risk_metrics": risk_metrics,
        "risk_metric_definitions": definitions,
        "factors": {"f1_limit_up": {"verdict": PreSurgeVerdict.ACCEPTED}},
        "event_date": date(2025, 1, 2),
    }

    serialized = adapter.serialize_verdict(verdict)

    assert serialized["risk_metrics"] == risk_metrics
    assert serialized["risk_metric_definitions"] == definitions
    assert serialized["coverage"] == coverage
    assert serialized["factors"]["f1_limit_up"]["verdict"] == "accepted"
    assert serialized["event_date"] == "2025-01-02"
    json.dumps(serialized, allow_nan=False)
    assert adapter.extract_coverage(serialized) == coverage
    assert adapter.extract_coverage({"status": "unavailable"}) is None


def test_build_request_consumes_benchmark_parameter():
    cohort = ["600000.SH"]
    request = PreSurgeAdapter().build_request(
        START,
        END,
        cohort,
        oos_start=OOS_START,
        cost_bps=None,
        parameters={
            "start": START,
            "oos_start": OOS_START,
            "end": END,
            "benchmark_symbol": "000001.SH",
            "cost_bps": 12.0,
        },
    )
    assert request.benchmark_symbol == "000001.SH"
