"""Responses API 协议适配层 (app/services/ai_responses.py) 的契约测试。

伪事件流/伪响应全部用 SimpleNamespace 鸭子类型, 只对齐 SDK 暴露的字段名
(event.type / event.delta / response.status / item.call_id 等) — 适配层
本身也只依赖这些字段, 与 _iter_openai_text 的宽松风格一致。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.api import settings as settings_api
from app.config import settings as app_settings
from app.services import ai_provider, ai_responses

# ---------------------------------------------------------------------------
# 事件 / 响应工厂
# ---------------------------------------------------------------------------


def _evt(type_: str, **kw):
    return SimpleNamespace(type=type_, **kw)


def _text_delta(s: str):
    return _evt("response.output_text.delta", delta=s)


def _reasoning_delta(s: str = "内部推理"):
    return _evt("response.reasoning_summary_text.delta", delta=s)


def _completed(output=None, status="completed", incomplete_reason=None):
    resp = SimpleNamespace(status=status, output=list(output or []), error=None, output_text="")
    if incomplete_reason is not None:
        resp.incomplete_details = SimpleNamespace(reason=incomplete_reason)
    return _evt("response.completed", response=resp)


async def _fake_stream(*events):
    for e in events:
        yield e


def _message_item(text: str):
    part = SimpleNamespace(type="output_text", text=text)
    return SimpleNamespace(type="message", content=[part])


def _call_item(call_id: str, name: str, arguments: str):
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=arguments)


class _FakeApiError(Exception):
    """带 status_code/body 的伪上游错误, 喂给重试判定逻辑。"""

    def __init__(self, status_code: int, body: dict):
        super().__init__(json.dumps(body))
        self.status_code = status_code
        self.body = body


def _fake_client(result=None, stream=None, error=None):
    """伪 AsyncOpenAI: client.responses.create(**kwargs)。返回 (client, calls)。"""
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        if stream is not None:
            return stream
        return result

    return SimpleNamespace(responses=SimpleNamespace(create=create)), calls


# ---------------------------------------------------------------------------
# 消息 / 工具转换
# ---------------------------------------------------------------------------


def test_chat_input_converts_tool_round_trip_shapes():
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "查一下茅台"},
        {
            "role": "assistant",
            "content": "先查行情",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_quote", "arguments": '{"symbol": "600519"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
    ]

    items = ai_responses.chat_input(messages)

    assert items == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "查一下茅台"},
        {"role": "assistant", "content": "先查行情"},
        {"type": "function_call", "call_id": "call_1", "name": "get_quote",
         "arguments": '{"symbol": "600519"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'},
    ]


def test_chat_input_plain_assistant_and_missing_role():
    items = ai_responses.chat_input([
        {"role": "assistant", "content": "纯文本回复"},
        {"content": "无角色消息"},
    ])
    assert items == [
        {"role": "assistant", "content": "纯文本回复"},
        {"role": "user", "content": "无角色消息"},
    ]


def test_responses_tools_flattens_and_passes_through_flat():
    chat_schema = [{
        "type": "function",
        "function": {"name": "get_quote", "description": "查行情", "parameters": {"type": "object"}},
    }]
    flat_schema = [{
        "type": "function",
        "name": "get_kline",
        "parameters": {"type": "object"},
    }]

    assert ai_responses.responses_tools(chat_schema) == [{
        "type": "function", "name": "get_quote",
        "description": "查行情", "parameters": {"type": "object"},
    }]
    # 已扁平的 schema 原样接受(个别网关直接吐扁平形态); description 缺省可省
    assert ai_responses.responses_tools(flat_schema) == [{
        "type": "function", "name": "get_kline", "parameters": {"type": "object"},
    }]
    # 非 dict / 无 function 体的条目静默跳过
    assert ai_responses.responses_tools([None, {"type": "other"}]) == []


# ---------------------------------------------------------------------------
# kwargs 与自适应重试
# ---------------------------------------------------------------------------


def test_responses_kwargs_maps_param_names(monkeypatch):
    stored = {"ai_provider": "openai", "ai_reasoning_effort": "medium"}
    monkeypatch.setattr(secrets_store, "load", lambda: stored)

    kwargs = ai_responses.responses_kwargs(temperature=0.3, max_tokens=1200)
    assert kwargs == {
        "max_output_tokens": 1200,
        "temperature": 0.3,
        "reasoning": {"effort": "medium"},
    }

    # 兼容 provider 不下发 reasoning (对齐 _openai_kwargs 的 provider 门控)
    stored["ai_provider"] = "openai_compat"
    kwargs = ai_responses.responses_kwargs(temperature=None, max_tokens=1200)
    assert kwargs == {"max_output_tokens": 1200}


def test_responses_kwargs_omits_none_max_tokens():
    assert ai_responses.responses_kwargs(temperature=0.5, max_tokens=None) == {"temperature": 0.5}


def test_responses_kwargs_carries_tools():
    kwargs = ai_responses.responses_kwargs(
        temperature=None, max_tokens=None,
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
    )
    assert kwargs["tools"] == [{"type": "function", "name": "f", "parameters": {"type": "object"}}]


def test_responses_retry_kwargs_drops_rejected_params():
    temperature_reject = _FakeApiError(400, {"error": {"message": "temperature only 1 is allowed"}})
    reasoning_reject = _FakeApiError(400, {"error": {"message": "Unsupported parameter: 'reasoning'"}})

    retry = ai_responses.responses_retry_kwargs(
        temperature_reject, {"temperature": 0.3, "reasoning": {"effort": "high"}},
    )
    assert retry == {"reasoning": {"effort": "high"}}

    retry = ai_responses.responses_retry_kwargs(
        reasoning_reject, {"temperature": 0.3, "reasoning": {"effort": "high"}},
    )
    assert retry == {"temperature": 0.3}

    # 非 400 / 无关 400 不重试
    assert ai_responses.responses_retry_kwargs(
        _FakeApiError(500, {"error": {"message": "boom"}}), {"temperature": 0.3},
    ) is None
    assert ai_responses.responses_retry_kwargs(
        _FakeApiError(400, {"error": {"message": "model not found"}}), {"temperature": 0.3},
    ) is None


# ---------------------------------------------------------------------------
# iter_text: 事件流归一化 (与 _iter_openai_text 的防御等价)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_text_yields_deltas_and_ignores_reasoning():
    stream = _fake_stream(
        _reasoning_delta(),
        _text_delta("完整"),
        _text_delta("正文"),
        _completed(),
    )
    assert [p async for p in ai_responses.iter_text(stream)] == ["完整", "正文"]


@pytest.mark.asyncio
async def test_iter_text_done_event_fallback_without_deltas():
    """个别网关只发 output_text.done 不发 delta: done 全量兜底产出一次。"""
    stream = _fake_stream(
        _evt("response.output_text.done", text="一次成型"),
        _completed(),
    )
    assert [p async for p in ai_responses.iter_text(stream)] == ["一次成型"]


@pytest.mark.asyncio
async def test_iter_text_rejects_length_truncated_partial():
    stream = _fake_stream(
        _text_delta("未完成正文"),
        _completed(status="incomplete", incomplete_reason="max_output_tokens"),
    )
    with pytest.raises(RuntimeError, match="输出达到长度上限"):
        async for _ in ai_responses.iter_text(stream):
            pass


@pytest.mark.asyncio
async def test_iter_text_rejects_reasoning_only_length_exhaustion():
    stream = _fake_stream(
        _reasoning_delta(),
        _completed(status="incomplete", incomplete_reason="max_output_tokens"),
    )
    with pytest.raises(RuntimeError, match="推理达到输出长度上限"):
        async for _ in ai_responses.iter_text(stream):
            pass


@pytest.mark.asyncio
async def test_iter_text_rejects_stream_without_content():
    with pytest.raises(RuntimeError, match="未返回正文内容"):
        async for _ in ai_responses.iter_text(_fake_stream(_completed())):
            pass


@pytest.mark.asyncio
async def test_iter_text_failed_event_raises_with_detail():
    resp = SimpleNamespace(
        status="failed", output=[], error=SimpleNamespace(message="quota exceeded"),
    )
    stream = _fake_stream(_evt("response.failed", response=resp))
    with pytest.raises(RuntimeError, match="quota exceeded"):
        async for _ in ai_responses.iter_text(stream):
            pass


# ---------------------------------------------------------------------------
# 非流式: run_text / run_message_once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_text_extracts_text_with_and_without_helper_attr(monkeypatch):
    resp = SimpleNamespace(
        status="completed",
        output_text="聚合文本",
        output=[_message_item("聚合文本")],
        error=None,
    )
    client, calls = _fake_client(result=resp)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    assert await ai_responses.run_text(
        [{"role": "user", "content": "hi"}], temperature=0.3, max_tokens=100, timeout=5,
    ) == "聚合文本"
    assert calls[0]["input"] == [{"role": "user", "content": "hi"}]
    assert calls[0]["max_output_tokens"] == 100

    # output_text 便利属性缺失时逐 item 聚合
    resp2 = SimpleNamespace(status="completed", output=[_message_item("逐条"), _message_item("聚合")])
    client2, _ = _fake_client(result=resp2)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client2)
    assert await ai_responses.run_text(
        [{"role": "user", "content": "hi"}], temperature=None, max_tokens=None, timeout=5,
    ) == "逐条聚合"


@pytest.mark.asyncio
async def test_run_text_incomplete_raises(monkeypatch):
    resp = SimpleNamespace(
        status="incomplete",
        output_text="半截",
        output=[],
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    client, _ = _fake_client(result=resp)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    with pytest.raises(RuntimeError, match="输出达到长度上限"):
        await ai_responses.run_text(
            [{"role": "user", "content": "hi"}], temperature=None, max_tokens=10, timeout=5,
        )


@pytest.mark.asyncio
async def test_run_message_once_returns_chat_shape_calls(monkeypatch):
    resp = SimpleNamespace(
        status="completed",
        output_text="",
        output=[_call_item("call_1", "get_quote", '{"symbol":"600519"}')],
    )
    client, calls = _fake_client(result=resp)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    message = await ai_responses.run_message_once(
        [{"role": "user", "content": "查"}],
        temperature=0.3, max_tokens=None, timeout=5,
        tools=[{"type": "function", "function": {"name": "get_quote"}}],
    )
    tc = message.tool_calls[0]
    assert (tc.id, tc.function.name, tc.function.arguments) == (
        "call_1", "get_quote", '{"symbol":"600519"}',
    )
    # 工具 schema 以扁平形态下发
    assert calls[0]["tools"] == [{"type": "function", "name": "get_quote"}]


@pytest.mark.asyncio
async def test_run_message_once_retries_without_rejected_temperature(monkeypatch):
    ok_resp = SimpleNamespace(status="completed", output_text="OK", output=[_message_item("OK")])
    error = _FakeApiError(400, {"error": {"message": "temperature only 1 is allowed"}})
    client, calls = _fake_client(result=ok_resp, error=error)
    # 首次抛 400, 摘掉 temperature 后重试成功
    state = {"raised": False}

    async def create(**kwargs):
        calls.append(kwargs)
        if not state["raised"]:
            state["raised"] = True
            raise error
        return ok_resp

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    text = await ai_responses.run_message_once(
        [{"role": "user", "content": "hi"}], temperature=0.3, max_tokens=None, timeout=5,
    )
    assert text.content == "OK"
    assert calls[0]["temperature"] == 0.3
    assert "temperature" not in calls[1]


# ---------------------------------------------------------------------------
# stream_round: 助手工具轮事件流
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_round_accumulates_tool_calls_from_fragments(monkeypatch):
    stream = _fake_stream(
        _evt("response.output_item.added",
             item=SimpleNamespace(id="fc_1", type="function_call", call_id="call_1",
                                  name="get_quote", arguments="")),
        _text_delta("先查一下"),
        _evt("response.function_call_arguments.delta", item_id="fc_1", delta='{"sym'),
        _evt("response.function_call_arguments.delta", item_id="fc_1", delta='bol":"600519"}'),
        _evt("response.output_item.done",
             item=SimpleNamespace(id="fc_1", type="function_call", call_id="call_1",
                                  name="get_quote", arguments='{"symbol":"600519"}')),
        _completed(output=[_call_item("call_1", "get_quote", '{"symbol":"600519"}')]),
    )
    client, _ = _fake_client(stream=stream)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    events = [e async for e in ai_responses.stream_round(
        [{"role": "user", "content": "查茅台"}],
        [{"type": "function", "function": {"name": "get_quote"}}],
        temperature=0.3, timeout=5,
    )]

    assert events[0] == {"type": "text", "delta": "先查一下"}
    assert events[-1] == {
        "type": "round_end",
        "tool_calls": [{"id": "call_1", "name": "get_quote", "arguments": '{"symbol":"600519"}'}],
        "finish_reason": "tool_calls",
    }


@pytest.mark.asyncio
async def test_stream_round_completed_output_is_authoritative(monkeypatch):
    """completed.response.output 优先于分片累积(网关乱序/缺 done 时兜底)。"""
    stream = _fake_stream(
        _evt("response.output_item.added",
             item=SimpleNamespace(id="fc_1", type="function_call", call_id="call_1",
                                  name="", arguments="")),
        _evt("response.function_call_arguments.delta", item_id="fc_1", delta="半截参数"),
        _completed(output=[_call_item("call_x", "get_kline", '{"days":5}')]),
    )
    client, _ = _fake_client(stream=stream)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    events = [e async for e in ai_responses.stream_round(
        [{"role": "user", "content": "查K线"}], [], temperature=None, timeout=5,
    )]
    assert events[-1]["tool_calls"] == [
        {"id": "call_x", "name": "get_kline", "arguments": '{"days":5}'},
    ]


@pytest.mark.asyncio
async def test_stream_round_text_only_round_ends_empty(monkeypatch):
    stream = _fake_stream(
        _text_delta("回答完毕"),
        _completed(),
    )
    client, _ = _fake_client(stream=stream)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    events = [e async for e in ai_responses.stream_round(
        [{"role": "user", "content": "hi"}], [], temperature=None, timeout=5,
    )]
    assert events == [
        {"type": "text", "delta": "回答完毕"},
        {"type": "round_end", "tool_calls": [], "finish_reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_stream_round_length_truncation_without_calls_raises(monkeypatch):
    stream = _fake_stream(
        _text_delta("半截"),
        _completed(status="incomplete", incomplete_reason="max_output_tokens"),
    )
    client, _ = _fake_client(stream=stream)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    with pytest.raises(RuntimeError, match="输出达到长度上限"):
        async for _ in ai_responses.stream_round(
            [{"role": "user", "content": "hi"}], [], temperature=None, timeout=5,
        ):
            pass


@pytest.mark.asyncio
async def test_stream_round_empty_response_raises(monkeypatch):
    client, _ = _fake_client(stream=_fake_stream(_completed()))
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    with pytest.raises(RuntimeError, match="未返回正文内容"):
        async for _ in ai_responses.stream_round(
            [{"role": "user", "content": "hi"}], [], temperature=None, timeout=5,
        ):
            pass


# ---------------------------------------------------------------------------
# 协议配置与入口接线
# ---------------------------------------------------------------------------


def test_current_ai_protocol_config_and_fallback(monkeypatch):
    stored: dict = {}
    monkeypatch.setattr(secrets_store, "load", lambda: stored)

    assert ai_provider.current_ai_protocol() == "chat_completions"

    stored["ai_protocol"] = "responses"
    assert ai_provider.current_ai_protocol() == "responses"
    assert ai_provider.is_responses_protocol() is True

    stored["ai_protocol"] = "bogus"
    assert ai_provider.current_ai_protocol() == "chat_completions"

    # Codex CLI 恒为 chat_completions, 存储值不生效
    stored["ai_provider"] = "codex_cli"
    stored["ai_protocol"] = "responses"
    assert ai_provider.current_ai_protocol() == "chat_completions"


@pytest.mark.asyncio
async def test_generate_ai_text_routes_to_responses(monkeypatch):
    async def fake_run_text(messages, *, temperature, max_tokens, timeout):
        return "responses 回复"

    monkeypatch.setattr(secrets_store, "load", lambda: {"ai_protocol": "responses"})
    monkeypatch.setattr(ai_responses, "run_text", fake_run_text)

    text = await ai_provider.generate_ai_text(
        [{"role": "user", "content": "hi"}], temperature=0.3, max_tokens=100, timeout=5,
    )
    assert text == "responses 回复"


@pytest.mark.asyncio
async def test_stream_ai_text_routes_to_responses(monkeypatch):
    async def fake_stream_text(messages, *, temperature, max_tokens, timeout):
        yield "流式"
        yield "回复"

    monkeypatch.setattr(secrets_store, "load", lambda: {"ai_protocol": "responses"})
    monkeypatch.setattr(ai_responses, "stream_text", fake_stream_text)

    chunks = [c async for c in ai_provider.stream_ai_text(
        [{"role": "user", "content": "hi"}], temperature=0.5, max_tokens=100, timeout=5,
    )]
    assert chunks == ["流式", "回复"]


@pytest.mark.asyncio
async def test_assistant_round_routes_to_responses(monkeypatch):
    from app.custom.assistant import streaming

    async def fake_stream_round(messages, tools, *, temperature, timeout):
        yield {"type": "text", "delta": "助手回复"}
        yield {"type": "round_end", "tool_calls": [], "finish_reason": "stop"}

    monkeypatch.setattr(secrets_store, "load", lambda: {"ai_protocol": "responses"})
    monkeypatch.setattr(streaming, "is_responses_protocol", lambda: True)
    monkeypatch.setattr(streaming.ai_responses, "stream_round", fake_stream_round)

    events = [e async for e in streaming.stream_openai_round(
        [{"role": "user", "content": "hi"}], [], temperature=0.3, timeout=5,
    )]
    assert events == [
        {"type": "text", "delta": "助手回复"},
        {"type": "round_end", "tool_calls": [], "finish_reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_stream_round_awaits_async_close(monkeypatch):
    """SDK 2.x 的 AsyncStream.close 是协程: 必须真正 await, 而非 to_thread 丢弃。"""
    closed: list[bool] = []

    class _ClosableStream:
        def __init__(self, events):
            self._inner = _fake_stream(*events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self._inner.__anext__()

        async def close(self):
            closed.append(True)

    stream = _ClosableStream([_text_delta("OK"), _completed()])
    client, _ = _fake_client(stream=stream)
    monkeypatch.setattr(ai_responses, "responses_client", lambda timeout: client)
    monkeypatch.setattr(secrets_store, "load", lambda: {})

    events = [e async for e in ai_responses.stream_round(
        [{"role": "user", "content": "hi"}], [], temperature=None, timeout=5,
    )]
    assert closed == [True]
    assert events[-1]["finish_reason"] == "stop"


def test_save_ai_settings_persists_protocol(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.update(updates))
    monkeypatch.setattr(settings_api.secrets_store, "load", lambda: saved)
    original = app_settings.ai_protocol
    try:
        result = settings_api.save_ai_settings(settings_api.AiSettingsIn(
            provider="openai_compat",
            base_url="https://example.com/v1",
            api_key="sk-test",
            model="gpt-x",
            protocol="responses",
        ))
        assert saved["ai_protocol"] == "responses"
        assert result["ai_protocol"] == "responses"
        assert ai_provider.current_ai_protocol() == "responses"
    finally:
        app_settings.ai_protocol = original


def test_save_ai_settings_rejects_unknown_protocol(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.update(updates))
    monkeypatch.setattr(settings_api.secrets_store, "load", lambda: saved)

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        settings_api.save_ai_settings(settings_api.AiSettingsIn(
            provider="openai_compat", protocol="grpc",
        ))
    assert "ai_protocol" not in saved
