"""复盘系统提示词可配置 — preferences 键、生效链路与 API 端点。"""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import preferences
from app.services.market_recap import _SYSTEM_PROMPT, effective_system_prompt


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


# ---------------------------------------------------------------- 偏好键
def test_prompt_defaults_to_empty_when_unset():
    assert preferences.get_market_recap_system_prompt() == ""


def test_prompt_save_and_read_roundtrip_strips_whitespace():
    saved = preferences.set_market_recap_system_prompt("  用自己的口吻复盘  ")
    assert saved == "用自己的口吻复盘"
    assert preferences.get_market_recap_system_prompt() == "用自己的口吻复盘"


def test_prompt_empty_or_none_restores_default_and_removes_key():
    preferences.set_market_recap_system_prompt("自定义提示词")
    assert preferences.set_market_recap_system_prompt("   ") == ""
    assert preferences.get_market_recap_system_prompt() == ""
    # 恢复默认应删除键, 而不是留空串脏值
    assert "market_recap_system_prompt" not in preferences.load()

    preferences.set_market_recap_system_prompt("自定义提示词")
    assert preferences.set_market_recap_system_prompt(None) == ""
    assert preferences.get_market_recap_system_prompt() == ""


def test_prompt_rejects_overlong_text():
    with pytest.raises(ValueError):
        preferences.set_market_recap_system_prompt("字" * (preferences.MARKET_RECAP_PROMPT_MAX_CHARS + 1))
    # 拒绝后不落盘
    assert preferences.get_market_recap_system_prompt() == ""


def test_prompt_dirty_non_string_value_falls_back_to_empty():
    preferences.save({"market_recap_system_prompt": 123})
    assert preferences.get_market_recap_system_prompt() == ""


# ---------------------------------------------------------------- 生效链路
def test_effective_prompt_falls_back_to_builtin_default():
    assert effective_system_prompt() == _SYSTEM_PROMPT


def test_effective_prompt_uses_custom_and_reverts_after_clear():
    preferences.set_market_recap_system_prompt("我的自定义复盘提示词")
    assert effective_system_prompt() == "我的自定义复盘提示词"
    preferences.set_market_recap_system_prompt("")
    assert effective_system_prompt() == _SYSTEM_PROMPT


async def test_stream_passes_custom_prompt_to_llm(monkeypatch, tmp_path):
    """复盘流把自定义提示词作为 system message 下发; 清空后回退内置默认。"""
    from app.services import market_recap

    overview = {
        "as_of": "2026-10-21",
        "indices": [],
        "emotion": {"score": 55, "label": "震荡"},
        "breadth": {"up": 100, "down": 80, "flat": 10},
        "amount": {"total": 1.5e12},
        "limit": {"limit_up": 50},
    }
    monkeypatch.setattr(market_recap, "build_market_overview", lambda *a, **k: overview)
    monkeypatch.setattr(
        "app.services.dragon_tiger.build_recap_context", lambda data_dir: "",
    )
    monkeypatch.setattr(
        "app.services.auction_benchmark.build_recap_context", lambda data_dir: "",
    )

    captured: list[list[dict]] = []

    async def _fake_stream(messages, **kwargs):
        captured.append(messages)
        yield "正文"

    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", _fake_stream)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    preferences.set_market_recap_system_prompt("自定义系统提示词A")
    events = [e async for e in market_recap.recap_market_stream(repo)]
    assert "done" in [json.loads(e).get("type") for e in events]
    assert captured[-1][0] == {"role": "system", "content": "自定义系统提示词A"}

    preferences.set_market_recap_system_prompt("")
    events = [e async for e in market_recap.recap_market_stream(repo)]
    assert "done" in [json.loads(e).get("type") for e in events]
    assert captured[-1][0] == {"role": "system", "content": _SYSTEM_PROMPT}


# ---------------------------------------------------------------- API 端点
def _client() -> TestClient:
    from app.api.settings import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_get_returns_custom_default_and_flag():
    resp = _client().get("/api/settings/preferences/market-recap-prompt")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"custom": "", "default": _SYSTEM_PROMPT, "customized": False}

    preferences.set_market_recap_system_prompt("自定义提示词")
    body = _client().get("/api/settings/preferences/market-recap-prompt").json()
    assert body["custom"] == "自定义提示词"
    assert body["customized"] is True
    assert body["default"] == _SYSTEM_PROMPT


def test_api_put_saves_and_empty_restores_default():
    client = _client()
    resp = client.put(
        "/api/settings/preferences/market-recap-prompt", json={"prompt": "  新提示词  "}
    )
    assert resp.status_code == 200
    assert resp.json() == {"custom": "新提示词", "customized": True}
    assert preferences.get_market_recap_system_prompt() == "新提示词"

    resp = client.put("/api/settings/preferences/market-recap-prompt", json={"prompt": ""})
    assert resp.status_code == 200
    assert resp.json() == {"custom": "", "customized": False}
    assert preferences.get_market_recap_system_prompt() == ""


def test_api_put_overlong_returns_400():
    resp = _client().put(
        "/api/settings/preferences/market-recap-prompt",
        json={"prompt": "字" * (preferences.MARKET_RECAP_PROMPT_MAX_CHARS + 1)},
    )
    assert resp.status_code == 400
    assert "过长" in resp.json()["detail"]
