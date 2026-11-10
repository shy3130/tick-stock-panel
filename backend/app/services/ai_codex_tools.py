"""Codex 结构化工具轮次。仅生成并校验请求，工具由各调用方执行。"""
from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from app.services import ai_provider


async def run_message_once(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    *,
    max_tokens: int | None = None,
    timeout: float = 240.0,
) -> SimpleNamespace:
    """通过 --output-schema 请求一轮答复, 完整校验后产出正文和工具请求。"""
    names = sorted({tool["function"]["name"] for tool in tools})
    if not names:
        raise ValueError("Codex CLI 工具调用需要至少一个工具 schema")
    output_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": names},
                        "arguments": {"type": "string"},
                    },
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["content", "tool_calls"],
        "additionalProperties": False,
    }
    request = [
        {
            "role": "system",
            "content": (
                "Generate exactly one assistant turn for the conversation in the JSON payload. "
                "Follow its system instructions. Return JSON matching the output schema. "
                "content is the user-facing answer; tool_calls lists requests for the host application "
                "to execute, with arguments encoded as a JSON object string. "
                "Use only tools declared in the payload. Do not use shell, files, network, or other "
                "Codex tools. If local data is needed, request a tool and wait for its role=tool result. "
                "Never invent tool results or claim an action succeeded before receiving its result. "
                "When ready to answer, set tool_calls to an empty array."
            ),
        },
        # 保留完整 assistant.tool_calls / tool_call_id, 不用只读取 content 的文本序列化。
        {"role": "user", "content": json.dumps({"messages": list(messages), "tools": list(tools)}, ensure_ascii=False)},
    ]
    try:
        raw = await ai_provider._run_codex_cli(
            request, max_tokens=max_tokens,
            timeout=max(timeout, ai_provider.CODEX_MIN_TIMEOUT_SECONDS), output_schema=output_schema,
        )
    except RuntimeError as exc:
        detail = str(exc)
        if "--output-schema" in detail and any(
            hint in detail.lower() for hint in ("unexpected argument", "unrecognized", "unknown")
        ):
            raise ai_provider.AIToolProtocolError(
                "请升级本机 Codex CLI: 工具调用需要支持 --output-schema 的版本"
            ) from exc
        raise

    invalid = "Codex CLI 工具响应格式无效, 请重试或检查模型是否支持结构化输出"
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ai_provider.AIToolProtocolError(invalid) from exc
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("content"), str)
        or not isinstance(response.get("tool_calls"), list)
    ):
        raise ai_provider.AIToolProtocolError(invalid)

    calls = []
    for call in response["tool_calls"]:
        if not isinstance(call, dict):
            raise ai_provider.AIToolProtocolError(invalid)
        name, arguments = call.get("name"), call.get("arguments")
        if not isinstance(name, str) or name not in names or not isinstance(arguments, str):
            raise ai_provider.AIToolProtocolError(invalid)
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ai_provider.AIToolProtocolError(invalid) from exc
        if not isinstance(args, dict):
            raise ai_provider.AIToolProtocolError(invalid)
        calls.append(SimpleNamespace(
            id=uuid.uuid4().hex, type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        ))

    content = response["content"]
    if not content.strip() and not calls:
        raise ai_provider.AIToolProtocolError("Codex CLI 未返回正文或工具请求")
    return SimpleNamespace(content=content, tool_calls=calls)
