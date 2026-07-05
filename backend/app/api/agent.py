from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import agent_tools
from app.services import agent_sessions
from app.services.agent_bus import get_bus
from app.services.agent_runner import run_agent_turn
from app.services.ai_provider import generate_ai_text

router = APIRouter(prefix="/api/agent", tags=["agent"])
_TASKS: dict[str, asyncio.Task] = {}


class AgentChatIn(BaseModel):
    message: str
    profile_id: str | None = None


class AgentSendIn(BaseModel):
    messages: list[dict]
    profile_id: str | None = None


class AgentSessionCreateIn(BaseModel):
    title: str | None = None


class AgentSessionRenameIn(BaseModel):
    title: str


@router.get("/tools")
def list_tools() -> dict:
    return {"tools": agent_tools.TOOLS}


def _data_dir(request: Request) -> Path:
    repo = getattr(request.app.state, "repo", None)
    store = getattr(repo, "store", None)
    data_dir = getattr(store, "data_dir", None)
    if data_dir is not None:
        return Path(data_dir)
    from app.config import settings
    return settings.data_dir


@router.get("/sessions")
def list_sessions(request: Request) -> dict:
    return {"sessions": agent_sessions.list_sessions(_data_dir(request))}


@router.post("/sessions")
def create_session(req: AgentSessionCreateIn, request: Request) -> dict:
    return agent_sessions.create_session(_data_dir(request), req.title)


@router.patch("/sessions/{session_id}")
def rename_session(session_id: str, req: AgentSessionRenameIn, request: Request) -> dict:
    item = agent_sessions.rename_session(_data_dir(request), session_id, req.title)
    if item is None:
        raise HTTPException(status_code=404, detail="session not found")
    return item


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request) -> dict:
    return {"deleted": agent_sessions.delete_session(_data_dir(request), session_id)}


@router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str, request: Request) -> dict:
    if agent_sessions.get_session(_data_dir(request), session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"messages": agent_sessions.read_messages(_data_dir(request), session_id)}


@router.post("/attempts/{attempt_id}/cancel")
async def cancel_attempt(attempt_id: str) -> dict:
    task = _TASKS.get(attempt_id)
    if task is None or task.done():
        return {"cancelled": False}
    task.cancel()
    return {"cancelled": True}


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
    ], profile_id=req.profile_id, temperature=0.2, max_tokens=1200)

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
    ], profile_id=req.profile_id, temperature=0.2, max_tokens=1600)
    return {"answer": answer, "tool": tool_req["tool"], "tool_result": result}


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: AgentSendIn, request: Request) -> dict:
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages empty")
    last = req.messages[-1]
    if last.get("role") != "user" or not isinstance(last.get("content"), str):
        raise HTTPException(status_code=400, detail="last message must be a user message with string content")

    data_dir = _data_dir(request)
    session = agent_sessions.get_session(data_dir, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    running_attempt_id = session.get("last_attempt_id")
    if session.get("last_attempt_status") == "running" and running_attempt_id:
        running_task = _TASKS.get(running_attempt_id)
        if running_task is not None and not running_task.done():
            raise HTTPException(status_code=409, detail="上一轮回复仍在运行，请稍后重试")

    stored = last.get("display_content")
    agent_sessions.append_message(
        data_dir,
        session_id,
        "user",
        stored if isinstance(stored, str) else last["content"],
    )

    attempt_id = f"agent_attempt_{uuid.uuid4().hex[:12]}"
    bus = get_bus()
    bus.begin(session_id)
    agent_sessions.set_attempt(data_dir, session_id, attempt_id, "running")

    task = asyncio.create_task(
        run_agent_turn(
            data_dir=data_dir,
            session_id=session_id,
            attempt_id=attempt_id,
            messages=req.messages,
            app_state=request.app.state,
            profile_id=req.profile_id,
            bus=bus,
        )
    )
    _TASKS[attempt_id] = task
    task.add_done_callback(lambda _t, aid=attempt_id: _TASKS.pop(aid, None))
    return {"attempt_id": attempt_id, "session_id": session_id}


@router.get("/sessions/{session_id}/stream")
async def watch_stream(session_id: str, request: Request):
    if agent_sessions.get_session(_data_dir(request), session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    bus = get_bus()

    async def gen():
        async for event in bus.subscribe(session_id):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
