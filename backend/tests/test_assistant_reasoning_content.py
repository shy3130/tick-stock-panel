"""DeepSeek 思考模式的工具调用: reasoning_content 必须随 assistant 消息回传。

DeepSeek 默认开思考模式, 携带 tools 的请求若没有把上一轮工具调用的
reasoning_content 原样回传, 上游返回 400 "The reasoning_content in the
thinking mode must be passed back to the API." (见 #524)。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessage

from app.custom.assistant import chat_service, streaming
from app.custom.assistant import tools as assistant_tools
from app.services import ai_provider


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate({
        "id": "chunk",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": finish_reason}],
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


async def _events(monkeypatch: pytest.MonkeyPatch, chunks: list[ChatCompletionChunk]) -> list[dict[str, Any]]:
    monkeypatch.setattr(streaming, "is_responses_protocol", lambda: False)
    monkeypatch.setattr(streaming, "_client", lambda timeout: _FakeClient(chunks))
    monkeypatch.setattr(streaming, "current_ai_model", lambda: "deepseek-v4-pro")
    monkeypatch.setattr(streaming, "_build_kwargs", lambda temperature: {})
    return [e async for e in streaming.stream_openai_round([{"role": "user", "content": "hi"}], [])]


async def test_stream_round_end_carries_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    events = await _events(monkeypatch, [
        _chunk({"reasoning_content": "先看看"}),
        _chunk({"reasoning_content": "有哪些策略"}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "list_strategies", "arguments": "{}"}}]},
               finish_reason="tool_calls"),
    ])

    end = events[-1]
    assert end["type"] == "round_end"
    assert end["reasoning_content"] == "先看看有哪些策略"
    assert [c["name"] for c in end["tool_calls"]] == ["list_strategies"]


async def test_stream_round_end_without_reasoning_has_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    events = await _events(monkeypatch, [
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "list_strategies", "arguments": "{}"}}]},
               finish_reason="tool_calls"),
    ])

    assert "reasoning_content" not in events[-1]


async def test_stream_round_end_keeps_empty_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    # 上游显式返回空串时也要保留字段, 与"未返回该字段"区分开。
    events = await _events(monkeypatch, [
        _chunk({"reasoning_content": ""}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "list_strategies", "arguments": "{}"}}]},
               finish_reason="tool_calls"),
    ])

    assert events[-1]["reasoning_content"] == ""


@pytest.mark.parametrize("reasoning", ["先看看有哪些策略", ""])
async def test_chat_service_sends_reasoning_content_back(monkeypatch: pytest.MonkeyPatch, reasoning: str) -> None:
    captured: list[list[dict[str, Any]]] = []
    script = [
        {"tool_calls": [{"id": "call_1", "name": "list_strategies", "arguments": "{}"}],
         "reasoning_content": reasoning},
        {"text": "你有 3 个策略"},
    ]

    async def fake_round(messages, tool_schemas, *, temperature=0.3, timeout=240.0):
        captured.append([dict(m) for m in messages])
        step = script.pop(0)
        if step.get("text"):
            yield {"type": "text", "delta": step["text"]}
        end = {"type": "round_end", "tool_calls": step.get("tool_calls", []), "finish_reason": "stop"}
        if "reasoning_content" in step:
            end["reasoning_content"] = step["reasoning_content"]
        yield end

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "result": {}}

    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", fake_round)
    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", fake_execute)

    lines = [line async for line in chat_service.chat_stream(
        history=[{"role": "user", "content": "我有哪些策略?"}],
    )]
    assert [json.loads(line) for line in lines if '"error"' in line] == []
    assistant_msg = captured[1][-2]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["reasoning_content"] == reasoning


@pytest.mark.parametrize("reasoning", ["先跑一次回测", ""])
async def test_tool_loop_sends_reasoning_content_back(monkeypatch: pytest.MonkeyPatch, reasoning: str) -> None:
    replies = [
        ChatCompletionMessage.model_validate({
            "role": "assistant",
            "content": None,
            "reasoning_content": reasoning,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "run_backtest", "arguments": "{}"}}],
        }),
        ChatCompletionMessage.model_validate({"role": "assistant", "content": "完成"}),
    ]

    async def fake_once(messages, **kwargs):
        return replies.pop(0)

    async def fake_execute(name: str, args: dict) -> dict:
        return {"ok": True, "result": {}}

    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(ai_provider, "is_responses_protocol", lambda: False)
    monkeypatch.setattr(ai_provider, "_check_input_budget", lambda messages, *, max_tokens: None)
    monkeypatch.setattr(ai_provider, "_run_openai_message_once", fake_once)

    messages = await ai_provider.generate_ai_text_with_tools(
        [{"role": "user", "content": "迭代策略"}],
        [{"type": "function", "function": {"name": "run_backtest", "parameters": {}}}],
        execute_tool=fake_execute,
    )

    assert messages[1]["reasoning_content"] == reasoning
    assert "reasoning_content" not in messages[3]
