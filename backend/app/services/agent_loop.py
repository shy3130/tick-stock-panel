from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.services import agent_tools
from app.services.ai_provider import generate_ai_text, stream_ai_text

MAX_TOOL_ROUNDS = 5

_EXCLUDED_TOOLS: set[str] = set()
ALLOWED_AGENT_TOOLS = [t for t in agent_tools.TOOLS if t["name"] not in _EXCLUDED_TOOLS]
_ALLOWED_NAMES = {t["name"] for t in ALLOWED_AGENT_TOOLS}


def _tools_system() -> str:
    return (
        "You are TickFlow Stock Panel assistant. If you need a tool, reply with ONLY JSON "
        '{"tool":"<name>","args":{...}}. Otherwise answer the user directly. Available tools: '
        + json.dumps(ALLOWED_AGENT_TOOLS, ensure_ascii=False)
    )


def _parse_tool(text: str) -> dict | None:
    try:
        data = json.loads(text.strip())
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tool"), str):
        return None
    args = data.get("args")
    if args is not None and not isinstance(args, dict):
        return None
    return {"tool": data["tool"], "args": args or {}}


async def run_agent_stream(
    messages: list[dict],
    app_state: Any,
    profile_id: str | None = None,
    *,
    generate: Callable[..., Awaitable[str]] = generate_ai_text,
    stream: Callable[..., Any] = stream_ai_text,
) -> AsyncIterator[str]:
    """Multi-round tool loop plus final streamed answer. Yields NDJSON payload strings."""
    tool_ctx: list[dict] = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            convo = [{"role": "system", "content": _tools_system()}, *messages, *tool_ctx]
            decision = await generate(convo, profile_id=profile_id, temperature=0.2, max_tokens=1200)
            tool_req = _parse_tool(decision)
            if tool_req is None:
                break

            yield json.dumps(
                {"type": "tool_call", "name": tool_req["tool"], "args": tool_req["args"]},
                ensure_ascii=False,
            )
            if tool_req["tool"] not in _ALLOWED_NAMES:
                result = {"error": f"tool not allowed: {tool_req['tool']}"}
            else:
                try:
                    result = agent_tools.call_tool(tool_req["tool"], app_state, tool_req["args"])
                except ValueError as e:
                    result = {"error": str(e)}

            yield json.dumps(
                {"type": "tool_result", "name": tool_req["tool"], "result": result},
                ensure_ascii=False,
            )
            tool_ctx += [
                {"role": "assistant", "content": decision},
                {"role": "user", "content": "Tool result:\n" + json.dumps(result, ensure_ascii=False)},
            ]

        answer_msgs = [
            {"role": "system", "content": "Answer the user concisely using any tool results above."},
            *messages,
            *tool_ctx,
        ]
        async for delta in stream(answer_msgs, profile_id=profile_id, temperature=0.4, max_tokens=1600):
            if delta:
                yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)
    except Exception as e:
        yield json.dumps({"type": "error", "message": f"Agent 失败: {e}"}, ensure_ascii=False)
