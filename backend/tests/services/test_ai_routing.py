from __future__ import annotations

import asyncio

import pytest

from app.services import ai_routing
from app.services.ai_structured.models import AIUsage, GenerateResponse


def test_route_policy_persistence_defaults_and_normalization(monkeypatch, tmp_path):
    from app.config import settings
    from app.services import preferences

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    default = preferences.get_ai_route_policy()
    assert default == {"allow_profile_fallback": False, "fallback_profile_ids": []}

    saved = preferences.set_ai_route_policy(True, ["p1", "p2", "p1", "", "p3"])
    assert saved["allow_profile_fallback"] is True
    assert saved["fallback_profile_ids"] == ["p1", "p2", "p3"]


def test_validate_route_policy_rejects_unknown_id():
    with pytest.raises(ValueError, match="p_missing"):
        ai_routing.validate_route_policy(True, ["p1", "p_missing"], {"p1", "p2"})

    ok = ai_routing.validate_route_policy(False, ["p2", "p1", "p2"], {"p1", "p2"})
    assert ok.fallback_profile_ids == ["p2", "p1"]


def test_build_fallback_chain_primary_first_dedup(monkeypatch):
    policy = ai_routing.RoutePolicy(allow_profile_fallback=True, fallback_profile_ids=["p2", "p3", "p2"])
    chain = ai_routing.build_fallback_chain("p1", policy, {"p1", "p2", "p3"})
    assert chain == ["p1", "p2", "p3"]

    # primary missing → only allowlist
    chain2 = ai_routing.build_fallback_chain(None, policy, {"p2", "p3"})
    assert chain2 == ["p2", "p3"]


def test_health_registry_cooldown_and_ewma():
    reg = ai_routing.ProfileHealthRegistry(base_cooldown_s=1.0, max_cooldown_s=2.0, ewma_alpha=0.5)
    reg.record_failure("p1", "timeout")
    assert reg.is_in_cooldown("p1") is True
    reg.record_failure("p1", "quota")
    assert reg.is_in_cooldown("p1") is True
    health = reg.get_health("p1")
    assert health["consecutive_failures"] == 2
    assert health["last_error_category"] == "quota"
    assert "api_key" not in str(health)

    reg.record_success("p1", 100.0)
    assert reg.is_in_cooldown("p1") is False
    assert reg.get_health("p1")["consecutive_failures"] == 0
    reg.record_success("p1", 200.0)
    assert reg.get_health("p1")["latency_ewma_ms"] == 150.0


def test_health_registry_ignores_non_fallback_categories():
    reg = ai_routing.ProfileHealthRegistry()
    reg.record_failure("p1", "malformed")
    reg.record_failure("p1", "cancelled")
    assert reg.get_health("p1")["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_fallback_disabled_calls_primary_only(monkeypatch):
    from app.services import ai_provider

    calls: list[str | None] = []

    async def fake_single(messages, **kwargs):
        calls.append(kwargs.get("profile_id"))
        return GenerateResponse(text="ok", profile_id=kwargs.get("profile_id"))

    monkeypatch.setattr(ai_provider, "_generate_for_single_profile", fake_single)
    monkeypatch.setattr(
        ai_provider.ai_routing,
        "load_route_policy",
        lambda: ai_routing.RoutePolicy(allow_profile_fallback=False, fallback_profile_ids=["p2"]),
    )

    resp = await ai_provider.generate_ai_text_with_meta([{"role": "user", "content": "x"}], profile_id="p1")
    assert calls == ["p1"]
    assert resp.fallback_used is False


@pytest.mark.asyncio
async def test_fallback_enabled_tries_allowlist_in_order_and_accumulates_usage(monkeypatch):
    from app.services import ai_provider

    calls: list[str | None] = []

    async def fake_single(messages, **kwargs):
        pid = kwargs.get("profile_id")
        calls.append(pid)
        if pid == "p1":
            raise TimeoutError("upstream timeout")
        return GenerateResponse(
            text="ok",
            usage=AIUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            profile_id=pid,
            model="m2",
        )

    monkeypatch.setattr(ai_provider, "_generate_for_single_profile", fake_single)
    monkeypatch.setattr(
        ai_provider.ai_routing,
        "load_route_policy",
        lambda: ai_routing.RoutePolicy(allow_profile_fallback=True, fallback_profile_ids=["p2"]),
    )
    monkeypatch.setattr(ai_provider.ai_profiles, "list_profile_ids", lambda: ["p1", "p2"])
    monkeypatch.setattr(
        ai_provider.ai_routing,
        "get_health_registry",
        lambda: ai_routing.ProfileHealthRegistry(),
    )

    resp = await ai_provider.generate_ai_text_with_meta([{"role": "user", "content": "x"}], profile_id="p1")
    assert calls == ["p1", "p2"]
    assert resp.profile_id == "p2"
    assert resp.primary_profile_id == "p1"
    assert resp.fallback_used is True
    assert resp.fallback_reason == "timeout"
    assert resp.usage.total_tokens == 5


@pytest.mark.asyncio
async def test_cancelled_never_triggers_fallback(monkeypatch):
    from app.services import ai_provider

    calls: list[str | None] = []

    async def fake_single(messages, **kwargs):
        calls.append(kwargs.get("profile_id"))
        raise asyncio.CancelledError()

    monkeypatch.setattr(ai_provider, "_generate_for_single_profile", fake_single)
    monkeypatch.setattr(
        ai_provider.ai_routing,
        "load_route_policy",
        lambda: ai_routing.RoutePolicy(allow_profile_fallback=True, fallback_profile_ids=["p2"]),
    )
    monkeypatch.setattr(ai_provider.ai_profiles, "list_profile_ids", lambda: ["p1", "p2"])

    with pytest.raises(asyncio.CancelledError):
        await ai_provider.generate_ai_text_with_meta([{"role": "user", "content": "x"}], profile_id="p1")
    assert calls == ["p1"]


def test_settings_route_policy_roundtrip(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import settings as settings_api
    from app.config import settings
    from app.services import ai_profiles

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    ai_profiles.create_profile(name="A", provider="openai_compat", api_key="sk-a", model="m-a")
    ai_profiles.create_profile(name="B", provider="openai_compat", api_key="sk-b", model="m-b")
    ids = ai_profiles.list_profile_ids()

    app = FastAPI()
    app.include_router(settings_api.router)
    client = TestClient(app)

    payload = client.get("/api/settings/ai/profiles").json()
    assert payload["route_policy"]["allow_profile_fallback"] is False

    res = client.put("/api/settings/ai/route-policy", json={
        "allow_profile_fallback": True,
        "fallback_profile_ids": [ids[1]],
    })
    assert res.status_code == 200
    assert res.json()["route_policy"]["fallback_profile_ids"] == [ids[1]]

    bad = client.put("/api/settings/ai/route-policy", json={
        "allow_profile_fallback": True,
        "fallback_profile_ids": ["p_unknown"],
    })
    assert bad.status_code == 400
