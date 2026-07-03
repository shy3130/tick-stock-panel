from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import agent_tools
from app.services.ai_provider import generate_ai_text

router = APIRouter(prefix="/api/agent", tags=["agent"])


class AgentChatIn(BaseModel):
    message: str


@router.get("/tools")
def list_tools() -> dict:
    return {"tools": agent_tools.TOOLS}


@router.post("/chat")
async def chat(req: AgentChatIn, request: Request) -> dict:
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message empty")

    system = (
        "You are TickFlow Stock Panel assistant. "
        "You may answer directly. If you need a tool, return only JSON like "
        '{"tool":"list_strategies","args":{}}. Available tools: '
        + json.dumps(agent_tools.TOOLS, ensure_ascii=False)
    )
    first = await generate_ai_text([
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ], temperature=0.2, max_tokens=1200)

    tool_req = _parse_tool_request(first)
    if tool_req is None:
        return {"answer": first, "tool": None}

    try:
        result = agent_tools.call_tool(tool_req["tool"], request.app.state, tool_req.get("args") or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    answer = await generate_ai_text([
        {"role": "system", "content": "Answer the user using the tool result. Be concise."},
        {"role": "user", "content": message},
        {"role": "assistant", "content": first},
        {"role": "user", "content": "Tool result:\n" + json.dumps(result, ensure_ascii=False)},
    ], temperature=0.2, max_tokens=1600)
    return {"answer": answer, "tool": tool_req["tool"], "tool_result": result}


def _parse_tool_request(text: str) -> dict | None:
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
