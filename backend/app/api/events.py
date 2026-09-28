"""开放事件流 — SSE 短期票据 + /api/events 订阅端点。

EventSource 规范不能携带 Authorization 头, Token 调用方走两步:
  POST /api/events/ticket   (Bearer, 任意 scope) → 一次性票据 (60s)
  GET  /api/events?ticket=… (EventSource)         → 按 scope 过滤的事件流

GET /api/events 在访问中间件白名单内 (票据即凭证, 不走密码会话);
票据校验失败立即 401, 成功则流式推送: hello 帧 → 事件 + 15s 心跳。
"""
from __future__ import annotations

import asyncio
import queue
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.services import event_tickets
from app.services.events import bus, sse_format

router = APIRouter(prefix="/api/events", tags=["events"])

_HEARTBEAT_S = 15.0
_POLL_S = 0.5


@router.post("/ticket")
def issue_ticket(request: Request):
    """Bearer Token (任意 scope) → 一次性 SSE 票据。"""
    # 认证与限流已在网关中间件完成 (scope 规则 "*"); 端点内再验一次
    # 明文是为了拿到 Token 记录、让票据继承 scope (不放大权限)。
    authz = request.headers.get("authorization", "")
    plaintext = authz[len("Bearer "):].strip() if authz.startswith("Bearer ") else ""

    from app.services import api_tokens

    record = api_tokens.verify_token(request.app.state.repo.store.data_dir, plaintext)
    if record is None:
        raise HTTPException(status_code=401, detail="API Token 无效或已吊销")
    try:
        ticket, ttl = event_tickets.issue(record.get("scopes", []))
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return {"ticket": ticket, "expires_in": int(ttl), "stream": "/api/events?ticket=" + ticket}


@router.get("")
async def event_stream(ticket: str = ""):
    """SSE 事件流; 票据一次性, 重连需 POST /ticket 换新。"""
    scopes = event_tickets.consume(ticket) if ticket else None
    if scopes is None:
        return JSONResponse(status_code=401, content={"detail": "票据无效、已使用或过期"})

    q = bus.subscribe()

    async def gen():
        allowed = ("*", *scopes)
        try:
            yield sse_format({"type": "hello", "scope": "*", "data": {"scopes": scopes}, "ts": time.time()})
            last_send = time.monotonic()
            while True:
                try:
                    event = q.get_nowait()
                except queue.Empty:
                    if time.monotonic() - last_send >= _HEARTBEAT_S:
                        yield ": ping\n\n"  # SSE 注释帧 — 保活, 不产生 EventSource 事件
                        last_send = time.monotonic()
                    await asyncio.sleep(_POLL_S)
                    continue
                if event["scope"] in allowed:
                    yield sse_format(event)
                    last_send = time.monotonic()
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
