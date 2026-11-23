"""把核心 Codex 工具轮次转换为助手的流事件；工具仍由助手确认并执行。"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.services.ai_codex_tools import run_message_once


async def stream_codex_round(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    *,
    temperature: float | None = 0.3,
    timeout: float = 240.0,
) -> AsyncIterator[dict[str, Any]]:
    message = await run_message_once(messages, tools, timeout=timeout)
    calls = [
        {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
        for call in message.tool_calls
    ]
    if message.content:
        yield {"type": "text", "delta": message.content}
    yield {"type": "round_end", "tool_calls": calls, "finish_reason": "tool_calls" if calls else "stop"}
