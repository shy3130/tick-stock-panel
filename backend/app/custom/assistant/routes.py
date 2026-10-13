"""AI 助手 HTTP 端点 — 薄层: 参数校验 + 流式响应映射。

POST /api/custom/assistant/chat      NDJSON 事件流(协议见模块 __init__)
GET  /api/custom/assistant/status     供应商配置状态(前端门控展示)
GET  /api/custom/assistant/suggests   空会话快捷指令
GET  /api/custom/assistant/sessions   会话历史(服务端, 跨设备)
PUT  /api/custom/assistant/sessions   会话历史整体保存(前端防抖写入)
POST /api/custom/assistant/actions/{call_id}/decision
                                     动作工具确认/拒绝(确认卡按钮回调)
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import sessions as assistant_sessions
from app.custom.assistant import tools as assistant_tools
from app.custom.assistant.chat_service import chat_stream
from app.services.ai_provider import (
    ai_configured,
    current_ai_model,
    current_ai_provider,
    is_codex_cli_provider,
)
from app.services.ndjson_heartbeat import with_heartbeat


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class AssistantContext(BaseModel):
    page: str = Field(default="", max_length=100)
    symbol: str = Field(default="", max_length=32)


class ChatRequest(BaseModel):
    messages: list[HistoryMessage] = Field(min_length=1, max_length=200)
    context: AssistantContext | None = None


class ActionDecisionRequest(BaseModel):
    approve: bool


class AssistantSessionsPayload(BaseModel):
    """前端 store.ts 的持久化载荷: 会话数组 + 活跃会话 id。

    sessions 用宽松的 list[dict] (消息形状由前端定义, 后端只裁剪条数),
    上限与前端一致, 防御异常客户端把文件写爆。字段名对齐前端 (activeId)。
    """

    sessions: list[dict] = Field(max_length=assistant_sessions.MAX_SESSIONS)
    active_id: str = Field(default="", max_length=64, alias="activeId")

    model_config = {"populate_by_name": True}


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/custom/assistant", tags=["custom-assistant"])

    @router.post("/chat")
    async def chat(request: Request, req: ChatRequest):
        engine = getattr(request.app.state, "strategy_engine", None)
        repo = getattr(request.app.state, "repo", None)
        data_dir = str(repo.store.data_dir) if repo is not None else None
        context = req.context.model_dump() if req.context else None

        async def gen():
            async for line in with_heartbeat(chat_stream(
                history=[m.model_dump() for m in req.messages],
                context=context,
                engine=engine,
                data_dir=data_dir,
                repo=repo,
                quote_service=getattr(request.app.state, "quote_service", None),
                depth_service=getattr(request.app.state, "depth_service", None),
                capabilities=getattr(request.app.state, "capabilities", None),
                financial_scheduler=getattr(request.app.state, "financial_scheduler", None),
            )):
                yield line + "\n"

        return StreamingResponse(
            gen(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/status")
    def status() -> dict:
        configured = ai_configured()
        return {
            "configured": configured,
            "provider": current_ai_provider(),
            "model": current_ai_model(),
            "supports_tools": configured and not is_codex_cli_provider(),
        }

    @router.get("/suggests")
    def suggests() -> dict:
        return {"suggests": assistant_tools.QUICK_SUGGESTS}

    @router.get("/sessions")
    def sessions(request: Request) -> dict:
        repo = getattr(request.app.state, "repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="数据目录不可用")
        return assistant_sessions.load_sessions(Path(repo.store.data_dir))

    @router.put("/sessions")
    def put_sessions(request: Request, req: AssistantSessionsPayload) -> dict:
        repo = getattr(request.app.state, "repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="数据目录不可用")
        n = assistant_sessions.save_sessions(
            Path(repo.store.data_dir),
            {"sessions": req.sessions, "activeId": req.active_id},
        )
        return {"ok": True, "sessions": n}

    @router.post("/actions/{call_id}/decision")
    async def action_decision(call_id: str, req: ActionDecisionRequest) -> dict:
        """确认卡回调: 唤醒挂起中的动作工具(批准执行或拒绝)。"""
        action = await assistant_actions.registry.resolve(call_id, req.approve)
        if action is None:
            raise HTTPException(status_code=404, detail="确认请求不存在或已过期")
        return {"ok": True, "call_id": call_id, "decision": "approved" if req.approve else "denied"}

    return router
