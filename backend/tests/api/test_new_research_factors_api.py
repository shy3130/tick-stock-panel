from contextlib import contextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.research as research_api
from app.services.daily_event_research.models import (
    UnavailabilityReason as DailyUnavailableReason,
    unavailable_response as unavailable_daily_response,
)
from app.services.retrieval_routing_research import (
    RoutingUnavailableReason,
    unavailable_routing_response,
)

REPO = object()
SYMBOLS_30 = [f"{index:06d}.SH" for index in range(1, 31)]


def client():
    app = FastAPI()
    app.state.repo = REPO
    app.include_router(research_api.router)
    return TestClient(app)


class Scope:
    canonical = object()
    market_facts = object()
    universe_reader = object()


@contextmanager
def scope(_repo):
    yield Scope()


def test_dugu_endpoint_owns_canonical_and_market_facts_pins(monkeypatch):
    canonical = SimpleNamespace(manifest=lambda: {"source_generations": {}})
    facts = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        research_api,
        "PublishedCanonicalDailyReader",
        SimpleNamespace(from_repository=lambda repo: canonical),
    )
    monkeypatch.setattr(
        research_api,
        "PublishedDailyMarketFactsReader",
        SimpleNamespace(from_canonical_manifest=lambda manifest: facts),
    )

    def evaluate(body, actual_canonical, actual_facts):
        assert actual_canonical is canonical
        assert actual_facts is facts
        return unavailable_daily_response(body, DailyUnavailableReason.NO_EVENTS)

    monkeypatch.setattr(research_api, "evaluate_daily_events", evaluate)
    response = client().post(
        "/api/research/factors/dugu-trend/evaluate",
        json={
            "symbols": ["000001.sz"],
            "start": "2024-01-01",
            "oos_start": "2024-06-01",
            "end": "2024-12-31",
            "variant": "ma_24_72",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["promoted"] is False


def test_dugu_reports_market_facts_unavailability(monkeypatch):
    canonical = SimpleNamespace(manifest=lambda: {"source_generations": {}})
    monkeypatch.setattr(
        research_api,
        "PublishedCanonicalDailyReader",
        SimpleNamespace(from_repository=lambda repo: canonical),
    )

    def fail_market_facts(_manifest):
        raise RuntimeError("markets generation missing")

    monkeypatch.setattr(
        research_api,
        "PublishedDailyMarketFactsReader",
        SimpleNamespace(from_canonical_manifest=fail_market_facts),
    )
    response = client().post(
        "/api/research/factors/dugu-trend/evaluate",
        json={
            "symbols": ["000001.SZ"],
            "start": "2024-01-01",
            "oos_start": "2024-06-01",
            "end": "2024-12-31",
            "variant": "ma_24_72",
        },
    )
    assert response.status_code == 200
    assert response.json()["unavailable_reason"] == "unavailable_market_facts"


def test_pre_surge_endpoint_passes_all_three_pins(monkeypatch):
    monkeypatch.setattr(research_api, "hold_firm_reader_scope", scope)
    captured = {}

    def evaluate(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "promoted": False}

    monkeypatch.setattr(research_api, "evaluate_pre_surge_production", evaluate)
    response = client().post(
        "/api/research/factors/pre-surge-features/evaluate",
        json={
            "symbols": ["000001.SZ"],
            "start": "2024-01-01",
            "oos_start": "2024-06-01",
            "end": "2024-12-31",
        },
    )
    assert response.status_code == 200
    assert captured["canonical_reader"] is Scope.canonical
    assert captured["market_facts_reader"] is Scope.market_facts
    assert captured["universe_reader"] is Scope.universe_reader


def test_escape_capability_exposes_intraday_requirements(monkeypatch):
    response = client().get("/api/research/escape-risk")
    payload = response.json()
    assert payload["signals"]["s1"] == "available"
    assert payload["signals"]["s8"] == "available"
    assert payload["signals"]["s9"] == "available"
    for signal in ("s2", "s3", "s4", "s5", "s6", "s7", "s10"):
        assert payload["signals"][signal] == "available"
    assert payload["runtime_requirements"]["s10"].endswith("pit_float_shares")

    canonical = object()
    monkeypatch.setattr(
        research_api,
        "PublishedCanonicalDailyReader",
        SimpleNamespace(from_repository=lambda repo: canonical),
    )
    monkeypatch.setattr(
        research_api,
        "evaluate_escape_risk_production",
        lambda **kwargs: {"status": "ok", "promoted": False},
    )
    post = client().post(
        "/api/research/factors/escape-risk/evaluate",
        json={
            "symbols": ["000001.SZ"],
            "start": "2024-01-01",
            "end": "2024-12-31",
        },
    )
    assert post.status_code == 200
    assert post.json() == {"status": "ok", "promoted": False}


def test_mera_endpoint_enforces_panel_gate_and_passes_frozen_request(monkeypatch):
    canonical = object()
    panel = object()
    monkeypatch.setattr(
        research_api,
        "PublishedCanonicalDailyReader",
        SimpleNamespace(from_repository=lambda repo: canonical),
    )

    def build(reader, symbols, start, end, *, feature_ids, label_horizon):
        assert reader is canonical
        assert len(symbols) == 30
        assert feature_ids
        assert label_horizon == 1
        return panel

    monkeypatch.setattr(research_api, "build_pinned_factor_panel", build)
    monkeypatch.setattr(
        research_api,
        "evaluate_retrieval_routing",
        lambda actual_panel, request: unavailable_routing_response(
            request,
            RoutingUnavailableReason.PANEL_COVERAGE,
            "test gate",
        ),
    )
    response = client().post(
        "/api/research/factors/mera-routing/evaluate",
        json={
            "symbols": SYMBOLS_30,
            "start": "2023-01-01",
            "end": "2024-12-31",
            "placebo_rounds": 20,
        },
    )
    assert response.status_code == 200
    assert response.json()["unavailable_reason"] == "unavailable_panel_coverage"

    too_small = client().post(
        "/api/research/factors/mera-routing/evaluate",
        json={
            "symbols": SYMBOLS_30[:29],
            "start": "2023-01-01",
            "end": "2024-12-31",
        },
    )
    assert too_small.status_code == 422


def test_n_shape_pullback_depth_endpoint_is_fail_closed_and_validates_threshold():
    response = client().post(
        "/api/research/factors/n-shape-pullback-depth/evaluate",
        json={
            "symbols": ["000001.sz"],
            "start": "2024-01-01",
            "end": "2024-12-31",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["promoted"] is False

    invalid = client().post(
        "/api/research/factors/n-shape-pullback-depth/evaluate",
        json={
            "symbols": ["000001.SZ"],
            "start": "2024-01-01",
            "end": "2024-12-31",
            "reversal_mode": "fixed_pct",
            "reversal_value": 1.0,
        },
    )
    assert invalid.status_code == 422


def test_negative_exclusion_capabilities_and_pinned_scope(monkeypatch):
    capability = client().get("/api/research/negative-exclusion")
    assert capability.status_code == 200
    assert capability.json()["classes"]["v1"] == "unavailable_definition_unverified"
    assert capability.json()["classes"]["v3"] == "unavailable_no_pit_announcement_source"

    monkeypatch.setattr(research_api, "hold_firm_reader_scope", scope)
    captured = {}

    def evaluate(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "promoted": False}

    monkeypatch.setattr(
        research_api,
        "evaluate_negative_exclusion_production",
        evaluate,
    )
    response = client().post(
        "/api/research/factors/negative-exclusion/evaluate",
        json={
            "symbols": ["000001.sz"],
            "start": "2024-01-01",
            "oos_start": "2024-06-01",
            "end": "2024-12-31",
            "enabled_classes": ["v2", "v4", "v5"],
        },
    )
    assert response.status_code == 200
    assert captured["canonical_reader"] is Scope.canonical
    assert captured["market_facts_reader"] is Scope.market_facts
    assert captured["universe_reader"] is Scope.universe_reader
    assert captured["symbols"] == ["000001.SZ"]

    unavailable_class = client().post(
        "/api/research/factors/negative-exclusion/evaluate",
        json={
            "symbols": ["000001.SZ"],
            "start": "2024-01-01",
            "oos_start": "2024-06-01",
            "end": "2024-12-31",
            "enabled_classes": ["v1"],
        },
    )
    assert unavailable_class.status_code == 422
