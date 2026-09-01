from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.services.doji_patterns.models import (
    DOJI_FACTOR_IDS,
    DojiFactorResult,
    DojiProvenance,
    DojiResponse,
    DojiStatus,
    DojiVerdict,
)
from app.services.full_market_adapters import doji as doji_adapter
from app.services.full_market_research import RunnerContext
from app.services.hold_firm_patterns.adapters import ProductionReaderScopeUnavailable
from app.services.hold_firm_patterns.models import (
    CanonicalIdentity,
    DataIdentity,
    MarketFactsIdentity,
    UnavailabilityReason,
    UniverseDayIdentity,
    UniverseIdentity,
)

START = date(2024, 1, 1)
END = date(2025, 12, 31)
OOS_START = date(2025, 7, 1)
MARKET_DAYS = (date(2024, 1, 2), date(2024, 1, 3), date(2025, 7, 1))
COHORT = tuple(f"6{index:05d}.SH" for index in range(250))


class FakeCanonical:
    def __init__(self):
        self.preload_calls = []
        self.fail_preload = False

    def market_days(self, start: date, end: date):
        return tuple(day for day in MARKET_DAYS if start <= day <= end)

    def preload_panel(self, start, end, symbols):
        if self.fail_preload:
            raise OSError("preload panel unavailable")
        self.preload_calls.append((start, end, list(symbols)))


class FakeScope:
    canonical_fail_preload = False

    def __init__(self):
        self.canonical = FakeCanonical()
        self.canonical.fail_preload = type(self).canonical_fail_preload
        self.market_facts = object()
        self.universe_reader = object()


class RecordingBundleReader:
    instances: ClassVar[list[RecordingBundleReader]] = []
    fail_init = False
    fail_load = False

    def __init__(self, days, markets_reader):
        if type(self).fail_init:
            raise OSError("catalog unavailable")
        self.days = tuple(days)
        self.markets_reader = markets_reader
        self.load_symbols = None
        self.closed = 0
        type(self).instances.append(self)

    def load(self, symbols):
        self.load_symbols = list(symbols)
        if type(self).fail_load:
            raise RuntimeError("intraday query unavailable")
        return SimpleNamespace(
            rows={}, unavailable={(COHORT[0], MARKET_DAYS[0]): "intraday_rows_missing"}
        )

    def close(self):
        self.closed += 1


@pytest.fixture
def scope_and_bundle(monkeypatch):
    scopes = []
    RecordingBundleReader.instances = []
    RecordingBundleReader.fail_init = False
    RecordingBundleReader.fail_load = False
    FakeScope.canonical_fail_preload = False

    @contextmanager
    def fake_scope(_repo):
        scope = FakeScope()
        scopes.append(scope)
        yield scope

    monkeypatch.setattr(doji_adapter, "production_reader_scope", fake_scope)
    monkeypatch.setattr(
        doji_adapter, "CatalogPinnedEscapeRiskIntradayReader", RecordingBundleReader
    )
    monkeypatch.setattr(
        doji_adapter,
        "production_scope_matches",
        lambda context, canonical, facts: True,
    )
    return scopes


def _provenance() -> DojiProvenance:
    identity = DataIdentity(
        canonical=CanonicalIdentity(
            generation="canonical-g",
            manifest_sha256="a" * 64,
            source_generations={"markets": "markets-g"},
            calendar_id="cn-a",
        ),
        markets=MarketFactsIdentity(generation="markets-g", manifest_sha256="b" * 64),
        universe=UniverseIdentity(
            generation="20240101T000000Z-0123456789abcdef",
            manifest_sha256="c" * 64,
            schema_version=2,
            artifact="universe_presence",
            rule_version="presence_v1",
            retrospective=True,
            status_filter="daily_market_row_present_exact_day",
            source_artifact="fstore_snapshot",
            source_generation="20240101T000000",
            source_manifest_sha256="d" * 64,
            day_identities=(UniverseDayIdentity(day=MARKET_DAYS[0], content_hash="e" * 64),),
        ),
    )
    return DojiProvenance(
        identities=identity,
        calendar_id="cn-a",
        parameters={},
        params_provenance={"definition": "test"},
        code_version="test",
    )


def _ok_response() -> DojiResponse:
    verdicts = {
        "doji_position_interaction": DojiVerdict.REJECTED,
        "gravestone_high": DojiVerdict.ACCEPTED,
        "t_bar_low": DojiVerdict.REJECTED,
        "next_day_confirmation": DojiVerdict.REJECTED,
        "tail_session_doji": DojiVerdict.UNAVAILABLE,
    }
    return DojiResponse(
        status=DojiStatus.OK,
        factors=[
            DojiFactorResult(
                factor_id=factor_id,
                parent_events=0,
                qualified_events=0,
                not_selected_events=0,
                verdict=verdicts[factor_id],
                diagnostics=(
                    {"minute_data": "unavailable"} if factor_id == "tail_session_doji" else {}
                ),
            )
            for factor_id in DOJI_FACTOR_IDS
        ],
        provenance=_provenance(),
        coverage={"requested_symbols": len(COHORT), "event_days": 2},
    )


def _request(adapter):
    return adapter.build_request(START, END, COHORT, oos_start=OOS_START, cost_bps=None)


def test_complete_cohort_is_passed_once_and_d5_owns_bundle(monkeypatch, scope_and_bundle):
    calls = []

    def fake_evaluator(request, reader, market_facts, universe_reader, intraday_bundle=None):
        calls.append((request, reader, market_facts, universe_reader, intraday_bundle))
        return _ok_response()

    monkeypatch.setattr(doji_adapter, "evaluate_doji_patterns", fake_evaluator)
    adapter = doji_adapter.DojiPatternsFullMarketAdapter()
    request = _request(adapter)

    evaluated = adapter.evaluate(RunnerContext(repo=object(), reader=object()), request)

    assert len(calls) == 1
    assert calls[0][0] is request
    assert calls[0][0].symbols == list(COHORT)
    assert len(calls[0][0].symbols) > 200
    assert calls[0][4] is not None

    scope = scope_and_bundle[0]
    reader = RecordingBundleReader.instances[0]
    assert reader.days == MARKET_DAYS
    assert reader.markets_reader is scope.market_facts
    assert reader.load_symbols == list(COHORT)
    assert reader.closed == 1

    serialized = adapter.serialize_verdict(evaluated)
    assert serialized["status"] == "ok"
    assert serialized["request"]["symbols"] == list(COHORT)
    assert serialized["d5_intraday"] == {
        "provided": True,
        "unavailable_symbol_days": 1,
    }
    assert adapter.extract_coverage(serialized) == serialized["coverage"]


def test_missing_minutes_only_degrades_d5_and_preserves_d1_d4(monkeypatch, scope_and_bundle):
    calls = []

    def fake_evaluator(request, reader, market_facts, universe_reader, intraday_bundle=None):
        calls.append(intraday_bundle)
        return _ok_response()

    monkeypatch.setattr(doji_adapter, "evaluate_doji_patterns", fake_evaluator)
    RecordingBundleReader.fail_init = True
    adapter = doji_adapter.DojiPatternsFullMarketAdapter()

    serialized = adapter.serialize_verdict(
        adapter.evaluate(RunnerContext(repo=object(), reader=object()), _request(adapter))
    )

    assert len(calls) == 1
    assert calls[0] is None
    assert serialized["status"] == "ok"
    by_id = {factor["factor_id"]: factor for factor in serialized["factors"]}
    assert set(by_id) == set(DOJI_FACTOR_IDS)
    assert by_id["doji_position_interaction"]["verdict"] == "rejected"
    assert by_id["gravestone_high"]["verdict"] == "accepted"
    assert by_id["t_bar_low"]["verdict"] == "rejected"
    assert by_id["next_day_confirmation"]["verdict"] == "rejected"
    assert by_id["tail_session_doji"]["verdict"] == "unavailable"
    assert serialized["request"]["symbols"] == list(COHORT)


def test_intraday_load_failure_closes_owned_reader_and_degrades_d5(monkeypatch, scope_and_bundle):
    calls = []

    def fake_evaluator(request, reader, market_facts, universe_reader, intraday_bundle=None):
        calls.append(intraday_bundle)
        return _ok_response()

    monkeypatch.setattr(doji_adapter, "evaluate_doji_patterns", fake_evaluator)
    RecordingBundleReader.fail_load = True
    adapter = doji_adapter.DojiPatternsFullMarketAdapter()

    serialized = adapter.serialize_verdict(
        adapter.evaluate(RunnerContext(repo=object(), reader=object()), _request(adapter))
    )

    assert calls == [None]
    assert RecordingBundleReader.instances[0].closed == 1
    assert serialized["status"] == "ok"
    assert serialized["d5_intraday"]["provided"] is False


def test_preload_panel_warms_full_cohort_once(monkeypatch, scope_and_bundle):
    calls = []

    def fake_evaluator(request, reader, market_facts, universe_reader, intraday_bundle=None):
        calls.append(intraday_bundle)
        return _ok_response()

    monkeypatch.setattr(doji_adapter, "evaluate_doji_patterns", fake_evaluator)
    adapter = doji_adapter.DojiPatternsFullMarketAdapter()
    adapter.evaluate(
        RunnerContext(repo=object(), reader=object()),
        _request(adapter),
    )

    assert len(calls) == 1
    scope = scope_and_bundle[0]
    assert scope.canonical.preload_calls == [
        (
            START - timedelta(days=400),
            END + timedelta(days=120),
            list(COHORT),
        )
    ]


def test_preload_failure_fails_whole_order_without_evaluator(monkeypatch, scope_and_bundle):
    calls = []

    def fake_evaluator(request, reader, market_facts, universe_reader, intraday_bundle=None):
        calls.append(request)
        return _ok_response()

    monkeypatch.setattr(doji_adapter, "evaluate_doji_patterns", fake_evaluator)
    FakeScope.canonical_fail_preload = True
    adapter = doji_adapter.DojiPatternsFullMarketAdapter()

    serialized = adapter.serialize_verdict(
        adapter.evaluate(
            RunnerContext(repo=object(), reader=object()),
            _request(adapter),
        )
    )

    assert calls == []
    assert serialized["status"] == "unavailable"
    assert serialized["unavailable_reason"] == UnavailabilityReason.CANONICAL_READER.value
    assert serialized["factors"] == []
    assert RecordingBundleReader.instances == []


def test_scope_failure_is_explicit_order_level_unavailable(monkeypatch):
    @contextmanager
    def unavailable_scope(_repo):
        raise ProductionReaderScopeUnavailable(
            UnavailabilityReason.CANONICAL_READER, "canonical unavailable"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(doji_adapter, "production_reader_scope", unavailable_scope)
    adapter = doji_adapter.DojiPatternsFullMarketAdapter()

    serialized = adapter.serialize_verdict(
        adapter.evaluate(RunnerContext(repo=object(), reader=object()), _request(adapter))
    )

    assert serialized["status"] == "unavailable"
    assert serialized["unavailable_reason"] == UnavailabilityReason.CANONICAL_READER.value
    assert serialized["factors"] == []
    assert "d5_intraday" not in serialized


def test_build_request_defaults_and_validation():
    adapter = doji_adapter.DojiPatternsFullMarketAdapter()
    request = adapter.build_request(START, END, COHORT, oos_start=None, cost_bps=None)
    assert request.oos_start == OOS_START
    assert request.cost_bps == 10
    assert request.theta_body_ratio == 0.1

    with pytest.raises(ValueError, match="start < oos_start"):
        adapter.build_request(START, END, COHORT, oos_start=START, cost_bps=None)
    with pytest.raises(ValueError, match="canonical"):
        adapter.build_request(START, END, ["bad"], oos_start=OOS_START, cost_bps=None)


def test_build_request_consumes_theta_body_ratio_parameter():
    request = doji_adapter.DojiPatternsFullMarketAdapter().build_request(
        START,
        END,
        COHORT,
        oos_start=OOS_START,
        cost_bps=None,
        parameters={
            "start": START,
            "oos_start": OOS_START,
            "end": END,
            "theta_body_ratio": 0.25,
            "cost_bps": 12.0,
        },
    )
    assert request.theta_body_ratio == 0.25
