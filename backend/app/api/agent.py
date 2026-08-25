from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import agent_tools
from app.services import agent_sessions
from app.services.agent_bus import get_bus
from app.services.agent_runner import run_agent_turn
from app.services.ai_attempts import get_registry, new_attempt_id
from app.services.ai_structured import CancellationToken

router = APIRouter(prefix="/api/agent", tags=["agent"])


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


@router.get("/runtime")
def agent_runtime() -> dict:
    """只读展示当前 Agent 运行时。正式发行不提供切换开关。"""
    from app.config import settings

    runtime = str(settings.agent_runtime or "python").strip().lower()
    if runtime not in {"python", "pi"}:
        runtime = "python"
    return {"runtime": runtime, "switchable": False}



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
    registry = get_registry()
    cancelled = registry.cancel(attempt_id)
    return {"cancelled": cancelled}


@router.post("/chat")
async def chat(_req: AgentChatIn) -> dict:
    raise HTTPException(
        status_code=410,
        detail={
            "code": "agent_chat_removed",
            "message": "同步 /chat 已关闭，请使用 POST /api/agent/sessions 创建会话后走 /messages 与 /stream",
        },
    )


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
    if (
        session.get("last_attempt_status") == "running"
        and running_attempt_id
        and get_registry().is_running(running_attempt_id)
    ):
        raise HTTPException(status_code=409, detail="上一轮回复仍在运行，请稍后重试")

    stored = last.get("display_content")
    agent_sessions.append_message(
        data_dir,
        session_id,
        "user",
        stored if isinstance(stored, str) else last["content"],
    )

    attempt_id = new_attempt_id()
    bus = get_bus()
    bus.begin(session_id)
    agent_sessions.set_attempt(data_dir, session_id, attempt_id, "running")
    token = CancellationToken()

    task = asyncio.create_task(
        run_agent_turn(
            data_dir=data_dir,
            session_id=session_id,
            attempt_id=attempt_id,
            messages=req.messages,
            app_state=request.app.state,
            profile_id=req.profile_id,
            bus=bus,
            token=token,
        )
    )
    get_registry().register(attempt_id=attempt_id, task=task, token=token)
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
    """复用会话 Agent 的 JSON/DSML 降级解析，避免 /chat 展示模型控制标记。"""
    from app.services.agent_loop import _parse_tool

    return _parse_tool(text)
