from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import agent, financials, market_recap, settings as settings_api, stock_analysis, strategy
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


def test_ai_entry_request_models_accept_profile_id():
    assert strategy.AIGenerateRequest(prompt="x", profile_id="p").profile_id == "p"
    assert strategy.BuildRequest(step=1, profile_id="p").profile_id == "p"
    assert agent.AgentChatIn(message="x", profile_id="p").profile_id == "p"
    assert stock_analysis.AnalyzeRequest(symbol="000001.SZ", profile_id="p").profile_id == "p"
    assert financials.AnalyzeRequest(symbol="000001.SZ", profile_id="p").profile_id == "p"
    assert market_recap.AnalyzeRequest(profile_id="p").profile_id == "p"
