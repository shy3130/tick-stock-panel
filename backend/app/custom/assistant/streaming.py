"""OpenAI 兼容 SSE 流式工具调用 — 助手模块自建, 不改核心。

为什么不复用核心: generate_ai_text_with_tools 是非流式的(全部工具轮次
结束后一次性返回全文); stream_ai_text 流式但不支持 tools 协议。这里只
复用核心的公开配置读取(Key/模型/Base URL/URL 规范化), 自行驱动
stream=True + tools= 的对话: 文本 delta 逐段产出, 同一个流里累积
tool_calls 分片, 流结束时统一返回本轮结果, 由 chat_service 决定是否
执行工具并进入下一轮。

输出为归一化事件流:
- {"type": "text", "delta": str}             正文增量(可能多次)
- {"type": "round_end", "tool_calls": [...], "finish_reason": str}
  本轮结束; tool_calls 非空表示模型请求工具, 元素形如
  {"id", "name", "arguments"(JSON 字符串)}, 上游附带 extra_content 时
  (如 Gemini 的 thought_signature) 另有 "extra_content" 键, 需原样回传。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from openai import AsyncOpenAI

from app import secrets_store
from app.config import settings
from app.services.ai_provider import (
    OPENAI_PROVIDER,
    current_ai_model,
    current_ai_provider,
    current_openai_reasoning_effort,
    normalize_openai_base_url,
)

# 部分推理型模型把整段预算花在 reasoning_content 上, 以 200 + length 结束。
_LENGTH_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens"}


def _build_kwargs(temperature: float | None) -> dict[str, Any]:
    """与核心 _openai_kwargs 同语义的精简版: temperature/max_tokens/reasoning_effort。"""
    kwargs: dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if current_ai_provider() == OPENAI_PROVIDER:
        effort = current_openai_reasoning_effort()
        if effort:
            kwargs["reasoning_effort"] = effort
    return kwargs


def _retry_kwargs(exc: Exception, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """400 明确拒绝某个可选参数时移除后重试(流尚未开始, 安全), 一次一个。"""
    status = getattr(exc, "status_code", None)
    if status != 400:
        return None
    text = f"{getattr(exc, 'body', '')} {exc}".lower()
    retry = dict(kwargs)
    if "temperature" in retry and "temperature" in text:
        retry.pop("temperature")
        return retry
    if "reasoning_effort" in retry and "reasoning_effort" in text:
        retry.pop("reasoning_effort")
        return retry
    return None


def _client(timeout: float) -> AsyncOpenAI:
    ai_key = secrets_store.get_ai_key()
    if not ai_key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")
    user_agent = secrets_store.get_ai_config("ai_user_agent", "") or settings.ai_user_agent
    return AsyncOpenAI(
        api_key=ai_key,
        base_url=normalize_openai_base_url(
            secrets_store.get_ai_config("ai_base_url", settings.ai_base_url),
        ),
        timeout=timeout,
        max_retries=2,
        default_headers={"User-Agent": user_agent},
    )


async def stream_openai_round(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    *,
    temperature: float | None = 0.3,
    timeout: float = 240.0,
) -> AsyncIterator[dict[str, Any]]:
    """执行一轮流式补全: yield 文本 delta, 最后 yield 一个 round_end 事件。"""
    client = _client(timeout)
    model = current_ai_model()
    kwargs = _build_kwargs(temperature)
    kwargs["tools"] = list(tools)

    while True:
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=[dict(m) for m in messages],
                stream=True,
                **kwargs,
            )
            break
        except Exception as exc:
            retry = _retry_kwargs(exc, kwargs)
            if retry is not None:
                kwargs = retry
                continue
            raise

    content_seen = False
    reasoning_seen = False
    finish_reason = ""
    calls: dict[int, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []

    async for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        reason = getattr(choice, "finish_reason", None)
        if reason:
            finish_reason = str(reason)
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        if getattr(delta, "reasoning_content", None):
            reasoning_seen = True

        text = getattr(delta, "content", None)
        if text:
            content_seen = True
            yield {"type": "text", "delta": text}

        for tc in getattr(delta, "tool_calls", None) or []:
            idx = int(getattr(tc, "index", 0) or 0)
            tc_id = str(getattr(tc, "id", None) or "")
            entry = calls.get(idx)
            # Gemini 的兼容接口会把并行调用逐个整条下发且 index 相同(或缺省),
            # 只靠 index 累积会把两个调用的 name/arguments 拼成一个; 同一 index
            # 上出现不同的 id 即视为新的调用。
            if entry is None or (tc_id and entry["id"] and tc_id != entry["id"]):
                entry = {"id": "", "name": "", "arguments": ""}
                calls[idx] = entry
                ordered.append(entry)
            if tc_id:
                entry["id"] = tc_id
            # Gemini 3 在 extra_content.google.thought_signature 里返回思考签名,
            # 下一轮回传历史时缺了它会被 400 拒绝。
            extra = getattr(tc, "extra_content", None)
            if extra and "extra_content" not in entry:
                entry["extra_content"] = extra
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    entry["name"] += str(fn.name)
                if getattr(fn, "arguments", None):
                    entry["arguments"] += str(fn.arguments)

    # 让 SDK 流对象尽快释放(部分兼容网关要求显式 close)。
    close = getattr(stream, "close", None)
    if close is not None:
        await asyncio.to_thread(close)

    if calls:
        yield {
            "type": "round_end",
            "tool_calls": ordered,
            "finish_reason": finish_reason,
        }
        return

    if finish_reason in _LENGTH_FINISH_REASONS:
        raise RuntimeError("AI 输出达到长度上限, 内容不完整; 请提高输出 Token 上限后重试")
    if not content_seen:
        if reasoning_seen:
            raise RuntimeError("AI 仅返回推理内容, 未生成正文; 请检查模型配置或改用非推理模型")
        raise RuntimeError("AI 服务未返回正文内容; 请检查模型配置或稍后重试")
    yield {"type": "round_end", "tool_calls": [], "finish_reason": finish_reason}
