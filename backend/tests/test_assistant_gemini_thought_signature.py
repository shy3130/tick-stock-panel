"""Gemini 3 (OpenAI 兼容接口) 的工具调用: thought_signature 回传与并行调用拆分。

Gemini 3 在 tool_calls[].extra_content.google.thought_signature 返回思考签名,
下一轮回传 assistant 消息时缺了它, 上游直接 400 "Function call is missing a
thought_signature"。另外它的流式接口把并行调用逐个整条下发且 index 相同,
只按 index 累积会把工具名拼成 get_market_overviewget_sector_rotation。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessage

from app.custom.assistant import chat_service, streaming
from app.custom.assistant import tools as assistant_tools
from app.services import ai_provider

SIGNATURE = {"google": {"thought_signature": "c2lnLWE="}}


def _chunk(tool_calls: list[dict[str, Any]], finish_reason: str | None = None) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate({
        "id": "chunk",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gemini-3.8-flash",
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "tool_calls": tool_calls},
            "finish_reason": finish_reason,
        }],
    })


class _FakeStream:
    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeClient:
    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self.chat = self
        self.completions = self
        self._chunks = chunks

    async def create(self, **kwargs: Any) -> _FakeStream:
        return _FakeStream(self._chunks)


async def _round_end(monkeypatch: pytest.MonkeyPatch, chunks: list[ChatCompletionChunk]) -> dict[str, Any]:
    monkeypatch.setattr(streaming, "_client", lambda timeout: _FakeClient(chunks))
    monkeypatch.setattr(streaming, "current_ai_model", lambda: "gemini-3.8-flash")
    monkeypatch.setattr(streaming, "_build_kwargs", lambda temperature: {})
    events = [e async for e in streaming.stream_openai_round([{"role": "user", "content": "hi"}], [])]
    assert events[-1]["type"] == "round_end"
    return events[-1]


async def test_stream_keeps_thought_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    end = await _round_end(monkeypatch, [_chunk([{
        "index": 0,
        "id": "function-call-1",
        "type": "function",
        "function": {"name": "check_data_coverage", "arguments": "{}"},
        "extra_content": SIGNATURE,
    }], finish_reason="tool_calls")])

    assert end["tool_calls"] == [{
        "id": "function-call-1",
        "name": "check_data_coverage",
        "arguments": "{}",
        "extra_content": SIGNATURE,
    }]


async def test_stream_splits_parallel_calls_sharing_an_index(monkeypatch: pytest.MonkeyPatch) -> None:
    end = await _round_end(monkeypatch, [
        _chunk([{
            "index": 0,
            "id": "function-call-1",
            "type": "function",
            "function": {"name": "get_market_overview", "arguments": "{}"},
            "extra_content": SIGNATURE,
        }]),
        _chunk([{
            "index": 0,
            "id": "function-call-2",
            "type": "function",
            "function": {"name": "get_sector_rotation", "arguments": '{"days": 5}'},
        }], finish_reason="tool_calls"),
    ])

    assert [(c["id"], c["name"], c["arguments"]) for c in end["tool_calls"]] == [
        ("function-call-1", "get_market_overview", "{}"),
        ("function-call-2", "get_sector_rotation", '{"days": 5}'),
    ]
    assert end["tool_calls"][0]["extra_content"] == SIGNATURE
    assert "extra_content" not in end["tool_calls"][1]


async def test_stream_still_joins_fragments_of_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    end = await _round_end(monkeypatch, [
        _chunk([{"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "get_stock_quote", "arguments": '{"symbols": '}}]),
        _chunk([{"index": 0, "function": {"arguments": '["600519.SH"]}'}}]),
        _chunk([{"index": 1, "id": "call_2", "type": "function",
                 "function": {"name": "get_stock_daily", "arguments": "{}"}}], finish_reason="tool_calls"),
    ])

    assert [(c["id"], c["name"], c["arguments"]) for c in end["tool_calls"]] == [
        ("call_1", "get_stock_quote", '{"symbols": ["600519.SH"]}'),
        ("call_2", "get_stock_daily", "{}"),
    ]


async def test_chat_service_sends_thought_signature_back(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[dict[str, Any]]] = []
    script = [
        {"tool_calls": [{"id": "function-call-1", "name": "check_data_coverage",
                         "arguments": "{}", "extra_content": SIGNATURE}]},
        {"text": "数据是最新的"},
    ]

    async def fake_round(messages, tool_schemas, *, temperature=0.3, timeout=240.0):
        captured.append([dict(m) for m in messages])
        step = script.pop(0)
        if step.get("text"):
            yield {"type": "text", "delta": step["text"]}
        yield {"type": "round_end", "tool_calls": step.get("tool_calls", []), "finish_reason": "stop"}

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "result": {}}

    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", fake_round)
    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", fake_execute)

    lines = [line async for line in chat_service.chat_stream(
        history=[{"role": "user", "content": "帮我检查本地数据完整性"}],
    )]
    assert [json.loads(line) for line in lines if '"error"' in line] == []
    assistant_msg = captured[1][-2]
    assert assistant_msg["tool_calls"] == [{
        "id": "function-call-1",
        "type": "function",
        "function": {"name": "check_data_coverage", "arguments": "{}"},
        "extra_content": SIGNATURE,
    }]


async def test_tool_loop_sends_thought_signature_back(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        ChatCompletionMessage.model_validate({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "function-call-1",
                "type": "function",
                "function": {"name": "run_backtest", "arguments": "{}"},
                "extra_content": SIGNATURE,
            }],
        }),
        ChatCompletionMessage.model_validate({"role": "assistant", "content": "完成"}),
    ]

    async def fake_once(messages, **kwargs):
        return replies.pop(0)

    async def fake_execute(name: str, args: dict) -> dict:
        return {"ok": True, "result": {}}

    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(ai_provider, "_check_input_budget", lambda messages, *, max_tokens: None)
    monkeypatch.setattr(ai_provider, "_run_openai_message_once", fake_once)

    messages = await ai_provider.generate_ai_text_with_tools(
        [{"role": "user", "content": "迭代策略"}],
        [{"type": "function", "function": {"name": "run_backtest", "parameters": {}}}],
        execute_tool=fake_execute,
    )

    assert messages[1]["tool_calls"][0]["extra_content"] == SIGNATURE
