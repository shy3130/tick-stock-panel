"""OpenAI Responses API 协议适配 — 与 chat.completions 路径平行的第二协议。

设计约束(与 ai_provider._stream_openai / _iter_openai_text 对齐):
- kwargs 构建与 400 自适应重试同语义, 仅参数名不同:
  max_tokens→max_output_tokens, reasoning_effort→reasoning={"effort"}。
- 事件流归一化沿用 chat 版的防御: 长度截断 / 仅推理无正文 / 空响应都显式报错。
- 工具调用走 function_call output item + function_call_output 回填,
  对外仍暴露 chat 形态 (role:tool / tool_calls), 上层 (generate_ai_text_with_tools,
  assistant chat_service) 零改动; 转换在本模块进出两端完成。
- 本模块不做 provider 判定, 由 ai_provider.is_responses_protocol() 分流进入。

配置读取全部经 ai_provider 模块限定调用(非 from-import 名字绑定),
保证测试可 monkeypatch ai_provider.current_ai_provider 等。
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

from app.services import ai_provider
from app.services.ai_provider import Message

# Responses 的 incomplete_details.reason 与 status 语义对齐 chat 的 finish_reason。
_LENGTH_INCOMPLETE_REASONS = {"length", "max_tokens", "max_output_tokens"}
# 推理事件前缀: reasoning_summary_text.delta / reasoning_text.delta 等。
_REASONING_EVENT_PREFIXES = (
    "response.reasoning_summary",
    "response.reasoning_text",
    "response.reasoning.",
)


def responses_client(timeout: float):
    """复用核心的 AsyncOpenAI 客户端 (Key/Base URL/UA/重试策略一处维护)。"""
    ai_key = ai_provider.secrets_store.get_ai_key()
    if not ai_key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")
    return ai_provider._openai_client(ai_key, timeout)


# ---------------------------------------------------------------------------
# 消息与工具的双向转换 (chat 形态 ⇄ Responses input/tools)
# ---------------------------------------------------------------------------


def chat_input(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """chat messages → Responses input items。

    role:tool → function_call_output; assistant.tool_calls → function_call items;
    其余 (system/user/assistant 纯文本) 走 easy-input message 形态。
    Gemini 兼容层的 extra_content 在 Responses 协议下无意义, 静默丢弃。
    """
    items: list[dict[str, Any]] = []
    for m in messages:
        role = str(m.get("role") or "user")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(m.get("tool_call_id") or ""),
                "output": str(m.get("content") or ""),
            })
            continue
        tool_calls = m.get("tool_calls")
        if role == "assistant" and tool_calls:
            if m.get("content"):
                items.append({"role": "assistant", "content": str(m["content"])})
            for tc in tool_calls:
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "arguments": str(fn.get("arguments") or ""),
                })
            continue
        items.append({"role": role, "content": str(m.get("content") or "")})
    return items


def responses_tools(tools: Sequence[dict]) -> list[dict[str, Any]]:
    """chat 工具 schema → Responses 扁平工具 schema。

    chat: {"type":"function","function":{name,description,parameters}}
    responses: {"type":"function","name","description","parameters"}
    兼容已是扁平形态的输入(部分网关直接吐扁平 schema)。
    """
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            fn = t if t.get("type") == "function" else None
        if not fn:
            continue
        item: dict[str, Any] = {
            "type": "function",
            "name": str(fn.get("name") or ""),
        }
        if fn.get("description"):
            item["description"] = str(fn["description"])
        if fn.get("parameters") is not None:
            item["parameters"] = fn["parameters"]
        if item["name"]:
            out.append(item)
    return out


def responses_kwargs(
    *,
    temperature: float | None,
    max_tokens: int | None,
    tools: Sequence[dict] | None = None,
) -> dict[str, Any]:
    """构建 responses.create kwargs (与 _openai_kwargs 同语义, 参数名映射)。"""
    kwargs: dict[str, Any] = {}
    if max_tokens is not None:
        kwargs["max_output_tokens"] = max_tokens
    if temperature is not None:
        kwargs["temperature"] = temperature
    if ai_provider.current_ai_provider() == ai_provider.OPENAI_PROVIDER:
        effort = ai_provider.current_openai_reasoning_effort()
        if effort:
            kwargs["reasoning"] = {"effort": effort}
    if tools:
        kwargs["tools"] = responses_tools(tools)
    return kwargs


def _is_reasoning_rejected(exc: Exception) -> bool:
    """True if the upstream 400 specifically rejects the reasoning param."""
    if getattr(exc, "status_code", None) != 400:
        return False
    text = (ai_provider._openai_error_detail(exc) or str(exc)).lower()
    param = ai_provider._openai_error_param(exc)
    return param in ("reasoning", "reasoning.effort", "effort") or "reasoning" in text


def responses_retry_kwargs(exc: Exception, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Remove one explicitly rejected optional argument for a bounded retry."""
    retry = dict(kwargs)
    if "temperature" in retry and ai_provider._is_temperature_rejected(exc):
        retry.pop("temperature")
        return retry
    if "reasoning" in retry and _is_reasoning_rejected(exc):
        retry.pop("reasoning")
        return retry
    return None


# ---------------------------------------------------------------------------
# 终态检查 (completed/failed/incomplete → chat 版等价报错)
# ---------------------------------------------------------------------------


def _incomplete_reason(resp) -> str:
    details = getattr(resp, "incomplete_details", None)
    reason = getattr(details, "reason", None) if details is not None else None
    if reason:
        return str(reason)
    # 网关兼容: incomplete_details 为 dict 的情况
    if isinstance(details, dict):
        return str(details.get("reason") or "")
    return ""


def _response_error_detail(resp) -> str:
    err = getattr(resp, "error", None)
    if err is None:
        return "上游未返回错误详情"
    message = getattr(err, "message", None)
    if message is None and isinstance(err, dict):
        message = err.get("message")
    return ai_provider._compact_error_text(str(message or err)) or "上游未返回错误详情"


def _check_terminal_response(resp, *, content_seen: bool, reasoning_seen: bool) -> None:
    """completed/failed/incomplete 终态的防御性检查, 报错文案与 chat 版一致。"""
    status = str(getattr(resp, "status", "") or "")
    if status == "failed":
        raise RuntimeError(f"AI 服务请求失败: {_response_error_detail(resp)}")
    if status != "incomplete":
        return
    reason = _incomplete_reason(resp)
    if reason not in _LENGTH_INCOMPLETE_REASONS:
        return
    if reasoning_seen and not content_seen:
        raise RuntimeError(
            "AI 推理达到输出长度上限, 未生成正文; 请提高输出 Token 上限或改用非推理模型"
        )
    raise RuntimeError("AI 输出达到长度上限, 内容不完整; 请提高输出 Token 上限后重试")


def _raise_no_content(reasoning_seen: bool) -> None:
    if reasoning_seen:
        raise RuntimeError("AI 仅返回推理内容, 未生成正文; 请检查模型配置或改用非推理模型")
    raise RuntimeError("AI 服务未返回正文内容; 请检查模型配置或稍后重试")


# ---------------------------------------------------------------------------
# 非流式: 纯文本 / 工具循环单轮
# ---------------------------------------------------------------------------


def _response_text(resp) -> str:
    """聚合 Response 的全部 output_text (便利属性缺失时逐 item 提取)。"""
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text:
        return text
    parts: list[str] = []
    for item in getattr(resp, "output", None) or []:
        if str(getattr(item, "type", "") or "") != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if str(getattr(part, "type", "") or "") in ("output_text", "text"):
                value = getattr(part, "text", None)
                if value:
                    parts.append(str(value))
    return "".join(parts)


def _response_reasoning_seen(resp) -> bool:
    for item in getattr(resp, "output", None) or []:
        if str(getattr(item, "type", "") or "").startswith("reasoning"):
            return True
    return False


def _response_calls(resp) -> list[Any]:
    """output 里的 function_call item → chat 形态 tool_calls (SimpleNamespace)。"""
    calls = []
    for item in getattr(resp, "output", None) or []:
        if str(getattr(item, "type", "") or "") != "function_call":
            continue
        calls.append(SimpleNamespace(
            id=str(getattr(item, "call_id", None) or getattr(item, "id", "") or ""),
            type="function",
            function=SimpleNamespace(
                name=str(getattr(item, "name", "") or ""),
                arguments=str(getattr(item, "arguments", "") or ""),
            ),
        ))
    return calls


async def _create_with_retry(client, *, payload: dict[str, Any], stream: bool):
    kwargs = dict(payload)
    while True:
        try:
            return await client.responses.create(**kwargs, stream=stream)
        except Exception as exc:
            retry = responses_retry_kwargs(exc, kwargs)
            if retry is not None:
                kwargs = retry
                continue
            if ai_provider._is_openai_transport_error(exc):
                raise RuntimeError(ai_provider._format_openai_error(exc)) from exc
            raise


async def run_text(
    messages: Sequence[Message],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
) -> str:
    """非流式纯文本 (generate_ai_text 的 Responses 分支)。"""
    client = responses_client(timeout)
    payload = responses_kwargs(temperature=temperature, max_tokens=max_tokens)
    payload["model"] = ai_provider.current_ai_model()
    payload["input"] = chat_input(messages)
    resp = await _create_with_retry(client, payload=payload, stream=False)
    text = _response_text(resp).strip()
    _check_terminal_response(
        resp, content_seen=bool(text), reasoning_seen=_response_reasoning_seen(resp),
    )
    return text


async def run_message_once(
    messages: Sequence[Message],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
    tools: Sequence[dict] | None = None,
):
    """非流式工具单轮 (generate_ai_text_with_tools 的 Responses 分支)。

    返回 chat 形态 message 对象 (content + tool_calls), 核心循环零改动。
    """
    client = responses_client(timeout)
    payload = responses_kwargs(temperature=temperature, max_tokens=max_tokens, tools=tools)
    payload["model"] = ai_provider.current_ai_model()
    payload["input"] = chat_input(messages)
    resp = await _create_with_retry(client, payload=payload, stream=False)
    text = _response_text(resp)
    calls = _response_calls(resp)
    if not calls:
        # 无工具调用时才做终态/空正文防御; 有调用时由上层执行工具进入下一轮。
        _check_terminal_response(
            resp, content_seen=bool(text), reasoning_seen=_response_reasoning_seen(resp),
        )
    return SimpleNamespace(content=text, tool_calls=calls)


# ---------------------------------------------------------------------------
# 流式: 纯文本 (stream_ai_text 分支)
# ---------------------------------------------------------------------------


async def stream_text(
    messages: Sequence[Message],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
) -> AsyncIterator[str]:
    client = responses_client(timeout)
    payload = responses_kwargs(temperature=temperature, max_tokens=max_tokens)
    payload["model"] = ai_provider.current_ai_model()
    payload["input"] = chat_input(messages)
    stream = await _create_with_retry(client, payload=payload, stream=True)
    try:
        async for piece in iter_text(stream):
            yield piece
    except Exception as exc:
        if ai_provider._is_openai_transport_error(exc):
            raise RuntimeError(ai_provider._format_openai_error(exc)) from exc
        raise


def _is_reasoning_event(event_type: str) -> bool:
    return event_type.startswith(_REASONING_EVENT_PREFIXES)


async def iter_text(stream) -> AsyncIterator[str]:
    """Responses 事件流 → 文本增量, 防御与 _iter_openai_text 等价。"""
    content_seen = False
    reasoning_seen = False
    completed = None

    async for event in stream:
        event_type = str(getattr(event, "type", "") or "")
        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", None)
            if delta:
                content_seen = True
                yield str(delta)
        elif event_type == "response.output_text.done" and not content_seen:
            # 个别网关只发 done 不发 delta: done 携带全量文本, 兜底产出一次。
            text = getattr(event, "text", None)
            if text:
                content_seen = True
                yield str(text)
        elif _is_reasoning_event(event_type):
            reasoning_seen = True
        elif event_type in ("response.completed", "response.incomplete"):
            completed = getattr(event, "response", None)
        elif event_type in ("response.failed", "error"):
            resp = getattr(event, "response", None)
            if resp is not None and getattr(resp, "error", None) is not None:
                raise RuntimeError(f"AI 服务请求失败: {_response_error_detail(resp)}")
            err = getattr(event, "error", None)
            if err is not None:
                raise RuntimeError(f"AI 服务请求失败: {_response_error_detail(SimpleNamespace(error=err))}")

    if completed is not None:
        _check_terminal_response(completed, content_seen=content_seen, reasoning_seen=reasoning_seen)
    if not content_seen:
        _raise_no_content(reasoning_seen)


# ---------------------------------------------------------------------------
# 流式: 工具轮 (assistant stream_openai_round 分支)
# ---------------------------------------------------------------------------


def _event_item_id(event) -> str:
    """function_call 累积的 key: item_id 优先, 缺失时回退 output_index。"""
    item_id = getattr(event, "item_id", None)
    if item_id:
        return str(item_id)
    item = getattr(event, "item", None)
    if item is not None:
        item_id = getattr(item, "id", None)
        if item_id:
            return str(item_id)
    return f"#{getattr(event, 'output_index', 0)}"


def _completed_calls(resp) -> list[dict[str, Any]]:
    """completed.response.output 是权威工具调用列表 (顺序与完整性优于分片累积)。"""
    calls = []
    for item in getattr(resp, "output", None) or []:
        if str(getattr(item, "type", "") or "") != "function_call":
            continue
        calls.append({
            "id": str(getattr(item, "call_id", None) or getattr(item, "id", "") or ""),
            "name": str(getattr(item, "name", "") or ""),
            "arguments": str(getattr(item, "arguments", "") or ""),
        })
    return calls


async def stream_round(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    *,
    temperature: float | None = 0.3,
    timeout: float = 240.0,
) -> AsyncIterator[dict[str, Any]]:
    """一轮 Responses 流式补全, 输出与 assistant streaming.stream_openai_round 同构。"""
    client = responses_client(timeout)
    payload = responses_kwargs(temperature=temperature, max_tokens=None, tools=tools)
    payload["model"] = ai_provider.current_ai_model()
    payload["input"] = chat_input(messages)
    stream = await _create_with_retry(client, payload=payload, stream=True)

    content_seen = False
    reasoning_seen = False
    calls: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    final_calls: list[dict[str, Any]] | None = None
    completed = None

    async for event in stream:
        event_type = str(getattr(event, "type", "") or "")
        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", None)
            if delta:
                content_seen = True
                yield {"type": "text", "delta": str(delta)}
        elif event_type == "response.output_text.done" and not content_seen:
            text = getattr(event, "text", None)
            if text:
                content_seen = True
                yield {"type": "text", "delta": str(text)}
        elif _is_reasoning_event(event_type):
            reasoning_seen = True
        elif event_type == "response.output_item.added":
            item = getattr(event, "item", None)
            if item is not None and str(getattr(item, "type", "") or "") == "function_call":
                key = _event_item_id(event)
                entry = {"id": "", "name": "", "arguments": ""}
                calls[key] = entry
                ordered.append(entry)
                call_id = getattr(item, "call_id", None)
                if call_id:
                    entry["id"] = str(call_id)
                if getattr(item, "name", None):
                    entry["name"] = str(item.name)
        elif event_type == "response.function_call_arguments.delta":
            key = _event_item_id(event)
            entry = calls.get(key)
            if entry is None:
                entry = {"id": "", "name": "", "arguments": ""}
                calls[key] = entry
                ordered.append(entry)
            delta = getattr(event, "delta", None)
            if delta:
                entry["arguments"] += str(delta)
        elif event_type == "response.output_item.done":
            item = getattr(event, "item", None)
            if item is not None and str(getattr(item, "type", "") or "") == "function_call":
                # done item 是权威值, 整条覆盖累积分片 (网关分片乱序/缺失时兜底)。
                key = _event_item_id(event)
                entry = calls.get(key)
                if entry is None:
                    entry = {"id": "", "name": "", "arguments": ""}
                    calls[key] = entry
                    ordered.append(entry)
                call_id = getattr(item, "call_id", None)
                if call_id:
                    entry["id"] = str(call_id)
                if getattr(item, "name", None):
                    entry["name"] = str(item.name)
                if getattr(item, "arguments", None):
                    entry["arguments"] = str(item.arguments)
        elif event_type in ("response.completed", "response.incomplete"):
            completed = getattr(event, "response", None)
            if completed is not None:
                final_calls = _completed_calls(completed)
        elif event_type in ("response.failed", "error"):
            resp = getattr(event, "response", None)
            err = getattr(event, "error", None)
            if err is None and resp is not None:
                err = getattr(resp, "error", None)
            raise RuntimeError(
                f"AI 服务请求失败: {_response_error_detail(SimpleNamespace(error=err))}"
            )

    # 让 SDK 流对象尽快释放(部分兼容网关要求显式 close); SDK 2.x 的
    # AsyncStream.close 是协程, 必须区分同步/异步两种签名。
    close = getattr(stream, "close", None)
    if close is not None:
        if inspect.iscoroutinefunction(close):
            await close()
        else:
            await asyncio.to_thread(close)

    if final_calls is None:
        final_calls = ordered
    if final_calls:
        yield {"type": "round_end", "tool_calls": final_calls, "finish_reason": "tool_calls"}
        return

    if completed is not None:
        _check_terminal_response(completed, content_seen=content_seen, reasoning_seen=reasoning_seen)
    if not content_seen:
        _raise_no_content(reasoning_seen)
    yield {"type": "round_end", "tool_calls": [], "finish_reason": "stop"}
