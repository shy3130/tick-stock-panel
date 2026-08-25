from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import agent, financials, market_recap, stock_analysis, strategy
from app.api import settings as settings_api
from app.config import settings


def test_ai_profile_crud_masks_key_and_reassigns_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()
    app.include_router(settings_api.router)
    client = TestClient(app)

    res = client.post("/api/settings/ai/profiles", json={
        "name": "A",
        "provider": "openai_compat",
        "base_url": "https://x/v1",
        "api_key": "sk-secret",
        "model": "m-a",
    })
    assert res.status_code == 200
    profile_id = res.json()["id"]

    payload = client.get("/api/settings/ai/profiles").json()
    assert payload["default_id"] == profile_id
    assert payload["profiles"][0]["has_api_key"] is True
    assert "sk-secret" not in str(payload)

    assert client.put(f"/api/settings/ai/profiles/{profile_id}", json={"name": "A2", "api_key": ""}).status_code == 200
    assert client.post(f"/api/settings/ai/profiles/{profile_id}/default").status_code == 200
    assert client.delete(f"/api/settings/ai/profiles/{profile_id}").status_code == 200


def test_ai_profile_health_probe_targets_exact_profile_without_fallback(tmp_path, monkeypatch):
    from app.services.ai_structured.models import AIUsage, GenerateResponse

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()
    app.include_router(settings_api.router)
    client = TestClient(app)
    profile_id = client.post("/api/settings/ai/profiles", json={
        "name": "Probe",
        "provider": "openai_compat",
        "api_key": "sk-probe",
        "model": "probe-model",
    }).json()["id"]
    captured = {}

    async def fake_generate(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return GenerateResponse(
            text="OK",
            provider="openai_compat",
            profile_id=profile_id,
            model="probe-model",
            usage=AIUsage(prompt_tokens=4, completion_tokens=1, total_tokens=5),
        )

    monkeypatch.setattr("app.services.ai_provider.generate_ai_text_with_meta", fake_generate)
    response = client.post(f"/api/settings/ai/profiles/{profile_id}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == "probe-model"
    assert body["usage"]["total_tokens"] == 5
    assert body["latency_ms"] >= 0
    assert captured["profile_id"] == profile_id
    assert captured["allow_fallback"] is False
    assert captured["max_tokens"] == 8


def test_ai_profile_health_probe_rejects_unknown_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()
    app.include_router(settings_api.router)
    response = TestClient(app).post("/api/settings/ai/profiles/missing/test")
    assert response.status_code == 404


def test_ai_entry_request_models_accept_profile_id():
    assert strategy.AIGenerateRequest(prompt="x", profile_id="p").profile_id == "p"
    assert strategy.BuildRequest(step=1, profile_id="p").profile_id == "p"
    assert agent.AgentChatIn(message="x", profile_id="p").profile_id == "p"
    assert stock_analysis.AnalyzeRequest(symbol="000001.SZ", profile_id="p").profile_id == "p"
    assert financials.AnalyzeRequest(symbol="000001.SZ", profile_id="p").profile_id == "p"
    assert market_recap.AnalyzeRequest(profile_id="p").profile_id == "p"
