from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import agent_tools
from app.services import agent_sessions
from app.services.agent_loop import run_agent_stream
from app.services.ai_provider import generate_ai_text

router = APIRouter(prefix="/api/agent", tags=["agent"])
_ACTIVE_ATTEMPTS: set[str] = set()
_CANCELLED_ATTEMPTS: set[str] = set()


class AgentChatIn(BaseModel):
    message: str
    profile_id: str | None = None


class AgentStreamIn(BaseModel):
    messages: list[dict]
    profile_id: str | None = None
    session_id: str | None = None


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
def cancel_attempt(attempt_id: str) -> dict:
    if attempt_id not in _ACTIVE_ATTEMPTS:
        return {"cancelled": False}
    _CANCELLED_ATTEMPTS.add(attempt_id)
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


@router.post("/stream")
async def chat_stream(req: AgentStreamIn, request: Request):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages empty")
    data_dir = _data_dir(request)
    if req.session_id and agent_sessions.get_session(data_dir, req.session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")

    async def gen():
        assistant_chunks: list[str] = []
        assistant_saved = False
        attempt_id = f"agent_attempt_{uuid.uuid4().hex[:12]}" if req.session_id else None
        if req.session_id:
            _ACTIVE_ATTEMPTS.add(attempt_id)
            yield json.dumps({"type": "attempt_start", "attempt_id": attempt_id, "session_id": req.session_id}) + "\n"
            last = req.messages[-1]
            if last.get("role") == "user" and isinstance(last.get("content"), str):
                stored = last.get("display_content")
                agent_sessions.append_message(
                    data_dir,
                    req.session_id,
                    "user",
                    stored if isinstance(stored, str) else last["content"],
                )
        try:
            async for line in run_agent_stream(req.messages, request.app.state, req.profile_id):
                if attempt_id and attempt_id in _CANCELLED_ATTEMPTS:
                    yield json.dumps({"type": "cancelled", "attempt_id": attempt_id}) + "\n"
                    break
                if req.session_id:
                    try:
                        event = json.loads(line)
                    except Exception:
                        event = {}
                    if event.get("type") == "delta" and isinstance(event.get("content"), str):
                        assistant_chunks.append(event["content"])
                    elif event.get("type") == "error" and isinstance(event.get("message"), str):
                        assistant_chunks.append(f"[错误] {event['message']}")
                    elif event.get("type") == "done" and assistant_chunks:
                        agent_sessions.append_message(data_dir, req.session_id, "assistant", "".join(assistant_chunks))
                        assistant_saved = True
                yield line + "\n"
            if req.session_id and assistant_chunks and not assistant_saved:
                agent_sessions.append_message(data_dir, req.session_id, "assistant", "".join(assistant_chunks))
        finally:
            if attempt_id:
                _ACTIVE_ATTEMPTS.discard(attempt_id)
                _CANCELLED_ATTEMPTS.discard(attempt_id)

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
