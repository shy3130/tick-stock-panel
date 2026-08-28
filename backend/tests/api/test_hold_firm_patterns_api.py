from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import research
from app.api.research import router
from app.services.hold_firm_patterns import (
    FACTOR_IDS,
    CapabilityResult,
    FactorResult,
    HoldFirmResponse,
    HoldFirmStatus,
    HoldFirmVerdict,
    ProductionReaderScopeUnavailable,
    UnavailabilityReason,
)


_REPO = object()


def _client(repo=_REPO):
    app = FastAPI()
    app.state.repo = repo
    app.include_router(router)
    return TestClient(app)


def _body(**overrides):
    body = {
        "symbols": ["600519.SH", "000001.SZ"],
        "start": "2024-01-02",
        "oos_start": "2024-02-01",
        "end": "2024-03-01",
        "cost_bps": 10.0,
    }
    body.update(overrides)
    return body


class _Scope:
    canonical = object()
    market_facts = object()
    universe_reader = object()


def _scope_failure(detail="pin drift"):
    error = ProductionReaderScopeUnavailable.__new__(ProductionReaderScopeUnavailable)
    error.reason = UnavailabilityReason.CANONICAL_READER
    error.detail = detail
    return error


def _install_scope(monkeypatch, *, scope=None, error=None):
    scope = scope or _Scope()
    state = {"repo": None, "closed": 0}

    @contextmanager
    def fake_scope(repo):
        state["repo"] = repo
        if error is not None:
            raise error
        try:
            yield scope
        finally:
            state["closed"] += 1

    monkeypatch.setattr(research, "hold_firm_reader_scope", fake_scope)
    return scope, state


def _ok_response():
    return HoldFirmResponse(
        status=HoldFirmStatus.OK,
        factors=[
            FactorResult(
                factor_id=factor_id,
                parent_events=0,
                qualified_events=0,
                not_selected_events=0,
                selection_verdict=HoldFirmVerdict.UNAVAILABLE,
                holding_verdict=HoldFirmVerdict.UNAVAILABLE,
                verdict=HoldFirmVerdict.UNAVAILABLE,
            )
            for factor_id in FACTOR_IDS
        ],
    )


def test_evaluate_rejects_extra_fields(monkeypatch):
    _install_scope(monkeypatch)
    monkeypatch.setattr(
        research,
        "evaluate_hold_firm_patterns_v1",
        lambda *_args: pytest.fail("invalid request must not evaluate"),
    )
    response = _client().post(
        "/api/research/factors/hold-firm-patterns/evaluate", json=_body(horizon=20)
    )
    assert response.status_code == 422


def test_evaluate_rejects_invalid_dates_symbols_and_cost(monkeypatch):
    _install_scope(monkeypatch)
    client = _client()
    endpoint = "/api/research/factors/hold-firm-patterns/evaluate"
    assert client.post(endpoint, json=_body(oos_start="2024-01-02")).status_code == 422
    assert client.post(endpoint, json=_body(end="2024-01-31")).status_code == 422
    assert client.post(endpoint, json=_body(symbols=["600519"])).status_code == 422
    assert client.post(endpoint, json=_body(cost_bps=1001)).status_code == 422


def test_evaluate_normalizes_dates_symbols_and_passes_pinned_readers(monkeypatch):
    scope, state = _install_scope(monkeypatch)
    captured = {}
    expected = _ok_response()

    def fake_evaluate(request, reader, market_facts, universe_reader):
        captured.update(
            request=request,
            reader=reader,
            market_facts=market_facts,
            universe_reader=universe_reader,
        )
        return expected

    monkeypatch.setattr(research, "evaluate_hold_firm_patterns_v1", fake_evaluate)
    response = _client().post(
        "/api/research/factors/hold-firm-patterns/evaluate",
        json=_body(symbols=[" 600519.sh ", "000001.sz"]),
    )
    assert response.status_code == 200
    assert captured["request"].symbols == ["600519.SH", "000001.SZ"]
    assert captured["request"].start.isoformat() == "2024-01-02"
    assert captured["request"].oos_start.isoformat() == "2024-02-01"
    assert captured["request"].end.isoformat() == "2024-03-01"
    assert captured["reader"] is scope.canonical
    assert captured["market_facts"] is scope.market_facts
    assert captured["universe_reader"] is scope.universe_reader
    assert state == {"repo": _REPO, "closed": 1}


def test_evaluate_passes_unavailable_response_through(monkeypatch):
    _, state = _install_scope(monkeypatch)
    unavailable = HoldFirmResponse(
        status=HoldFirmStatus.UNAVAILABLE,
        unavailable_reason=UnavailabilityReason.CANONICAL_READER,
    )
    monkeypatch.setattr(research, "evaluate_hold_firm_patterns_v1", lambda *_args: unavailable)
    response = _client().post("/api/research/factors/hold-firm-patterns/evaluate", json=_body())
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["unavailable_reason"] == "unavailable_canonical_reader"
    assert response.json()["factors"] == []
    assert state["closed"] == 1


def test_evaluate_passes_ok_response_through(monkeypatch):
    _install_scope(monkeypatch)
    expected = _ok_response()
    monkeypatch.setattr(research, "evaluate_hold_firm_patterns_v1", lambda *_args: expected)
    response = _client().post("/api/research/factors/hold-firm-patterns/evaluate", json=_body())
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert [item["factor_id"] for item in response.json()["factors"]] == list(FACTOR_IDS)


def test_evaluate_maps_scope_failure_to_unavailable(monkeypatch):
    _install_scope(monkeypatch, error=_scope_failure())
    monkeypatch.setattr(
        research,
        "evaluate_hold_firm_patterns_v1",
        lambda *_args: pytest.fail("scope failure must not evaluate"),
    )
    response = _client().post("/api/research/factors/hold-firm-patterns/evaluate", json=_body())
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["unavailable_reason"] == "unavailable_canonical_reader"
    assert response.json()["factors"] == []


def test_reader_scope_closes_when_evaluator_raises(monkeypatch):
    _, state = _install_scope(monkeypatch)

    def boom(*_args):
        raise RuntimeError("evaluator exploded")

    monkeypatch.setattr(research, "evaluate_hold_firm_patterns_v1", boom)
    with pytest.raises(RuntimeError, match="evaluator exploded"):
        research.evaluate_hold_firm_patterns_factor(
            research.HoldFirmPatternsRequest.model_validate(_body()),
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_REPO))),
        )
    assert state["closed"] == 1


def test_capability_passes_readers_and_closes_scope(monkeypatch):
    scope, state = _install_scope(monkeypatch)
    captured = {}

    def fake_assess(reader, market_facts, universe_reader):
        captured.update(reader=reader, market_facts=market_facts, universe_reader=universe_reader)
        return CapabilityResult(status=HoldFirmStatus.OK)

    monkeypatch.setattr(research, "assess_hold_firm_capability", fake_assess)
    response = _client().get("/api/research/hold-firm-patterns")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert captured["reader"] is scope.canonical
    assert captured["market_facts"] is scope.market_facts
    assert captured["universe_reader"] is scope.universe_reader
    assert state["closed"] == 1


def test_capability_maps_scope_failure_to_unavailable(monkeypatch):
    _install_scope(monkeypatch, error=_scope_failure())
    monkeypatch.setattr(
        research,
        "assess_hold_firm_capability",
        lambda *_args: pytest.fail("scope failure must not assess"),
    )
    response = _client().get("/api/research/hold-firm-patterns")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["identities"] is None
    assert response.json()["problems"] == ["unavailable_canonical_reader: pin drift"]
