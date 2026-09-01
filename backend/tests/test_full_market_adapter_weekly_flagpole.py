from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services.full_market_adapters.weekly_flagpole import WeeklyFlagpoleAdapter
from app.services.full_market_research import RunnerContext
from app.services.weekly_flagpole.models import COST_BPS, OOS_START

START = date(2024, 1, 1)
END = date(2025, 1, 31)


def test_build_request_keeps_complete_cohort_in_one_request():
    adapter = WeeklyFlagpoleAdapter()
    cohort = ["600000.SH", "000001.SZ", "300750.SZ"]

    request = adapter.build_request(
        START,
        END,
        cohort,
        oos_start=None,
        cost_bps=None,
    )

    assert request.symbols == cohort
    assert request.symbols is not cohort
    assert request.oos_start == OOS_START
    assert request.cost_bps == COST_BPS


def test_evaluate_passes_full_request_and_index_reader_once(monkeypatch):
    adapter = WeeklyFlagpoleAdapter()
    cohort = ["600000.SH", "000001.SZ", "300750.SZ"]
    request = adapter.build_request(
        START,
        END,
        cohort,
        oos_start=date(2025, 7, 1),
        cost_bps=12.5,
    )
    events = []
    canonical_reader = SimpleNamespace(
        preload_panel=lambda start, end, *, symbols: events.append(("preload", start, end, symbols))
    )
    closed = []
    index_reader = SimpleNamespace(close=lambda: closed.append(True))
    repo = SimpleNamespace(index_daily_research_reader=index_reader)
    context = RunnerContext(repo=repo, reader=canonical_reader)
    calls = []
    expected = {"status": "ok"}

    def fake_evaluate(received_request, received_reader, *, index_reader=None):
        events.append(("evaluate",))
        calls.append((received_request, received_reader, index_reader))
        return expected

    monkeypatch.setattr(
        "app.services.weekly_flagpole.service.evaluate",
        fake_evaluate,
    )

    result = adapter.evaluate(context, request)

    assert result is expected
    assert calls == [(request, canonical_reader, index_reader)]
    assert calls[0][0].symbols == cohort
    assert events == [
        ("preload", START - timedelta(days=900), END + timedelta(days=250), request.symbols),
        ("evaluate",),
    ]
    assert closed == [True]


def test_preload_failure_propagates_without_fallback(monkeypatch):
    adapter = WeeklyFlagpoleAdapter()
    request = adapter.build_request(
        START,
        END,
        ["600000.SH", "000001.SZ"],
        oos_start=None,
        cost_bps=None,
    )
    closed = []
    service_calls = []

    def preload_panel(*args, **kwargs):
        raise RuntimeError("preload failed")

    index_reader = SimpleNamespace(close=lambda: closed.append(True))
    context = RunnerContext(
        repo=SimpleNamespace(index_daily_research_reader=index_reader),
        reader=SimpleNamespace(preload_panel=preload_panel),
    )

    def fake_evaluate(*args, **kwargs):
        service_calls.append((args, kwargs))
        return {"status": "should-not-run"}

    monkeypatch.setattr(
        "app.services.weekly_flagpole.service.evaluate",
        fake_evaluate,
    )

    with pytest.raises(RuntimeError, match="preload failed"):
        adapter.evaluate(context, request)

    assert service_calls == []
    assert closed == [True]


def test_evaluate_closes_index_reader_when_service_raises(monkeypatch):
    adapter = WeeklyFlagpoleAdapter()
    request = adapter.build_request(
        START,
        END,
        ["600000.SH"],
        oos_start=None,
        cost_bps=None,
    )
    closed = []
    index_reader = SimpleNamespace(close=lambda: closed.append(True))
    context = RunnerContext(
        repo=SimpleNamespace(index_daily_research_reader=index_reader),
        reader=object(),
    )

    def raise_evaluate(*args, **kwargs):
        raise RuntimeError("evaluation failed")

    monkeypatch.setattr(
        "app.services.weekly_flagpole.service.evaluate",
        raise_evaluate,
    )

    with pytest.raises(RuntimeError, match="evaluation failed"):
        adapter.evaluate(context, request)

    assert closed == [True]


def test_missing_canonical_reader_returns_unavailable_without_fallback():
    adapter = WeeklyFlagpoleAdapter()
    request = adapter.build_request(
        START,
        END,
        ["600000.SH", "000001.SZ"],
        oos_start=None,
        cost_bps=None,
    )

    result = adapter.evaluate(RunnerContext(repo=SimpleNamespace(), reader=None), request)

    assert result.status == "unavailable"
    assert result.unavailable_reasons == ["weekly_flagpole_research_reader_missing"]
    serialized = adapter.serialize_verdict(result)
    assert serialized["status"] == "unavailable"
    assert adapter.extract_coverage(serialized) is None
