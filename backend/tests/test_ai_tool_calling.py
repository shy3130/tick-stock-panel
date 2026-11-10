"""核心 AI 工具接口可供策略迭代和任意扩展调用，不依赖助手扩展。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.api import strategy as strategy_api
from app.services import ai_provider

TOOL = {
    "type": "function",
    "function": {
        "name": "extension_lookup",
        "description": "由调用方提供的扩展工具",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setattr(secrets_store, "load", lambda: {
        "ai_provider": "codex_cli", "ai_codex_model": "test-model",
    })
    monkeypatch.setattr(secrets_store, "get_ai_key", lambda: "")
    monkeypatch.setattr(ai_provider, "codex_cli_available", lambda: True)


async def test_codex_core_tool_loop_roundtrip_for_an_extension(codex, monkeypatch):
    requests = []
    replies = [
        {"content": "先查本地扩展", "tool_calls": [
            {"name": "extension_lookup", "arguments": '{"query":"demo"}'},
        ]},
        {"content": "查询完成", "tool_calls": []},
    ]

    async def cli(messages, **kwargs):
        requests.append((json.loads(messages[-1]["content"]), kwargs))
        return json.dumps(replies.pop(0), ensure_ascii=False)

    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"ok": True, "result": {"value": 42}}

    monkeypatch.setattr(ai_provider, "_run_codex_cli", cli)
    result = await ai_provider.generate_ai_text_with_tools(
        [{"role": "user", "content": "查询扩展"}], [TOOL],
        execute_tool=execute, max_rounds=2, max_tokens=None, timeout=900,
    )
    assert executed == [("extension_lookup", {"query": "demo"})]
    assert result[-1] == {"role": "assistant", "content": "查询完成"}
    assert json.loads(result[-2]["content"])["result"]["value"] == 42
    call = result[-3]["tool_calls"][0]
    assert result[-2]["tool_call_id"] == call["id"]
    assert requests[1][0]["messages"] == result[:-1]
    assert requests[0][0]["tools"] == [TOOL]
    assert requests[0][1]["timeout"] == 900
    assert requests[0][1]["output_schema"]["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["extension_lookup"]


@pytest.mark.parametrize("response", [
    "not json",
    '{"content":"", "tool_calls":[]}',
    '{"content":"", "tool_calls":[{"name":"unknown","arguments":"{}"}]}',
    '{"content":"", "tool_calls":[{"name":"extension_lookup","arguments":"[]"}]}',
])
async def test_codex_invalid_tool_round_never_executes(codex, monkeypatch, response):
    async def cli(*args, **kwargs):
        return response

    async def execute(*args):
        raise AssertionError("无效响应不能调用工具")

    monkeypatch.setattr(ai_provider, "_run_codex_cli", cli)
    with pytest.raises(RuntimeError, match=r"响应格式无效|未返回正文或工具请求"):
        await ai_provider.generate_ai_text_with_tools(
            [{"role": "user", "content": "查询"}], [TOOL], execute_tool=execute,
        )


async def test_codex_tool_loop_respects_round_budget(codex, monkeypatch):
    calls = []

    async def cli(*args, **kwargs):
        calls.append("model")
        return json.dumps({"content": "", "tool_calls": [
            {"name": "extension_lookup", "arguments": "{}"},
        ]})

    async def execute(*args):
        calls.append("tool")
        return {"ok": True, "result": {}}

    monkeypatch.setattr(ai_provider, "_run_codex_cli", cli)
    result = await ai_provider.generate_ai_text_with_tools(
        [], [TOOL], execute_tool=execute, max_rounds=1,
    )
    assert calls == ["model", "tool"]
    assert result[-1]["role"] == "tool"


def test_codex_strategy_status_exposes_tool_capability(codex):
    status = strategy_api.ai_status(SimpleNamespace())
    assert status["configured"] is True
    assert status["provider"] == "codex_cli"
    assert status["supports_tools"] is True


async def test_old_codex_cli_returns_an_actionable_protocol_error(codex, monkeypatch):
    async def cli(*args, **kwargs):
        raise RuntimeError("unexpected argument '--output-schema'")

    async def execute(*args):
        raise AssertionError("不兼容 CLI 不能调用工具")

    monkeypatch.setattr(ai_provider, "_run_codex_cli", cli)
    with pytest.raises(ai_provider.AIToolProtocolError, match=r"升级.*Codex CLI"):
        await ai_provider.generate_ai_text_with_tools([], [TOOL], execute_tool=execute)
