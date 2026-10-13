"""AI 草稿连通性测试端点 (POST /api/settings/ai/test) 的契约测试。

核心契约: 测试只读表单值, 与保存完全解耦 —— 测试失败不得写任何配置,
状态栏也不得把未验证的草稿展示为已生效。失败以 200 + ok=false 回报,
由前端内联展示, 不触发全局 toast。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import secrets_store
from app.api import settings as settings_api
from app.services import ai_provider


def test_probe_requires_base_url_model_and_key(monkeypatch):
    monkeypatch.setattr(secrets_store, "get_ai_key", lambda: "")

    with pytest.raises(ValueError, match="API 地址"):
        import asyncio
        asyncio.run(ai_provider.probe_ai_config(
            provider="openai_compat", api_key="sk-x", base_url="", model="m",
        ))
    with pytest.raises(ValueError, match="模型"):
        import asyncio
        asyncio.run(ai_provider.probe_ai_config(
            provider="openai_compat", api_key="sk-x", base_url="https://x.example/v1", model="",
        ))
    with pytest.raises(RuntimeError, match="API Key"):
        import asyncio
        asyncio.run(ai_provider.probe_ai_config(
            provider="openai_compat", api_key="", base_url="https://x.example/v1", model="m",
        ))


def test_probe_codex_unavailable_raises(monkeypatch):
    import asyncio

    monkeypatch.setattr(ai_provider, "codex_cli_available", lambda: False)
    with pytest.raises(RuntimeError, match="Codex CLI 不可用"):
        asyncio.run(ai_provider.probe_ai_config(
            provider="codex_cli", api_key="", base_url="", model="",
        ))


@pytest.mark.asyncio
async def test_probe_chat_protocol_uses_given_key_and_base(monkeypatch):
    calls: list[dict] = []

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            message = SimpleNamespace(content="OK")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class _Chat:
        completions = _Completions()

    captured_client: dict = {}

    class _Client:
        def __init__(self, **kwargs):
            captured_client.update(kwargs)
            self.chat = _Chat()

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)

    text = await ai_provider.probe_ai_config(
        provider="openai_compat",
        api_key="sk-draft",
        base_url="https://draft.example/v1",
        model="draft-model",
    )

    assert text == "OK"
    assert captured_client["api_key"] == "sk-draft"
    assert "draft.example" in captured_client["base_url"]
    assert calls == [{
        "model": "draft-model",
        "messages": [{"role": "user", "content": "Reply exactly: OK"}],
    }]


@pytest.mark.asyncio
async def test_probe_responses_protocol_calls_responses(monkeypatch):
    calls: list[dict] = []

    class _Responses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text="ok")

    class _Client:
        def __init__(self, **kwargs):
            self.responses = _Responses()

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)

    text = await ai_provider.probe_ai_config(
        provider="openai_compat",
        api_key="sk-draft",
        base_url="https://draft.example/v1",
        model="draft-model",
        protocol="responses",
    )

    assert text == "ok"
    assert calls[0]["model"] == "draft-model"
    assert calls[0]["input"] == [{"role": "user", "content": "Reply exactly: OK"}]


@pytest.mark.asyncio
async def test_test_endpoint_failure_reports_error_without_saving(monkeypatch):
    """关键回归: 测试失败 → ok=false 回报, 且不调用任何 secrets 写入。"""
    saved: list = []
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.append(updates))

    async def boom(**kwargs):
        raise RuntimeError("模型或接口地址不存在")

    monkeypatch.setattr(ai_provider, "probe_ai_config", boom)

    result = await settings_api.test_ai_settings(settings_api.AiTestIn(
        provider="openai_compat",
        base_url="https://bad.example/v1",
        api_key="sk-bad",
        model="no-such-model",
    ))

    assert result["ok"] is False
    assert "不存在" in result["error"]
    assert saved == []  # 未写入任何配置


@pytest.mark.asyncio
async def test_test_endpoint_falls_back_to_saved_key(monkeypatch):
    """表单 Key 留空时回退已保存 Key (改模型/协议不必重输)。"""
    seen: dict = {}

    async def fake_probe(**kwargs):
        seen.update(kwargs)
        return "OK"

    monkeypatch.setattr(ai_provider, "probe_ai_config", fake_probe)
    monkeypatch.setattr(settings_api.secrets_store, "get_ai_key", lambda: "sk-saved")

    result = await settings_api.test_ai_settings(settings_api.AiTestIn(
        provider="openai_compat",
        base_url="https://api.example/v1",
        api_key=None,
        model="m1",
        protocol="responses",
    ))

    assert result["ok"] is True
    assert seen["api_key"] == "sk-saved"
    assert seen["protocol"] == "responses"
