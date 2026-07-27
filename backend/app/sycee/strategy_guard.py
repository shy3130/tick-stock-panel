"""Resource guard for upstream Python strategy-authoring requests."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable
from threading import Lock

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.user_context import get_current_user
from app.sycee.strategy_security import is_ai_strategy_request, is_strategy_authoring_request

MAX_AUTHORING_BODY_BYTES = 512 * 1024
MAX_AUTHORING_FIELD_CHARS = {
    "code": 200_000,
    "current_code": 200_000,
    "prompt": 20_000,
    "rules": 20_000,
    "instruction": 20_000,
    "description": 5_000,
    "name": 200,
    "strategy_id": 128,
}
AI_REQUESTS_PER_WINDOW = 6
AI_RATE_WINDOW_SECONDS = 60.0
AI_MAX_CONCURRENT_REQUESTS = 2


class StrategyAIRequestLimiter:
    """Per-user sliding-window rate limit plus process-wide concurrency limit."""

    def __init__(
        self,
        *,
        max_requests: int = AI_REQUESTS_PER_WINDOW,
        window_seconds: float = AI_RATE_WINDOW_SECONDS,
        max_concurrent: int = AI_MAX_CONCURRENT_REQUESTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._max_concurrent = max_concurrent
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._active = 0
        self._lock = Lock()

    def acquire(self, key: str) -> tuple[str | None, int]:
        """Acquire one AI request slot; return an error code and retry delay on rejection."""

        now = self._clock()
        with self._lock:
            if self._active >= self._max_concurrent:
                return "STRATEGY_AI_BUSY", 1

            events = self._events[key]
            cutoff = now - self._window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self._max_requests:
                retry_after = max(1, math.ceil(events[0] + self._window_seconds - now))
                return "STRATEGY_AI_RATE_LIMIT", retry_after

            events.append(now)
            self._active += 1
            return None, 0

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            return int(value)
        except ValueError:
            return None
    return None


async def _buffer_body(receive: Receive, max_bytes: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        if message["type"] != "http.request":
            continue
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("body_too_large")
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _field_limit_error(body: bytes) -> tuple[str, int] | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (RecursionError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    for field, limit in MAX_AUTHORING_FIELD_CHARS.items():
        value = payload.get(field)
        if isinstance(value, str) and len(value) > limit:
            return field, limit
    return None


def _request_key(scope: Scope) -> str:
    user = get_current_user()
    if user is not None:
        return f"user:{user.id}"
    client = scope.get("client")
    host = client[0] if client else "unknown"
    return f"client:{host}"


async def _reject(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    detail: str,
    code: str,
    retry_after: int | None = None,
) -> None:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    response = JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code},
        headers=headers,
    )
    await response(scope, receive, send)


class SyceeStrategyAuthoringGuardMiddleware:
    """Bound strategy authoring payloads and AI request resource usage."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = MAX_AUTHORING_BODY_BYTES,
        limiter: StrategyAIRequestLimiter | None = None,
    ) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes
        self._limiter = limiter or StrategyAIRequestLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if not is_strategy_authoring_request(path, method):
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self._max_body_bytes:
            await _reject(
                scope,
                receive,
                send,
                status_code=413,
                detail=f"策略作者请求不得超过 {self._max_body_bytes} 字节",
                code="STRATEGY_AUTHORING_BODY_TOO_LARGE",
            )
            return

        try:
            body = await _buffer_body(receive, self._max_body_bytes)
        except ValueError:
            await _reject(
                scope,
                receive,
                send,
                status_code=413,
                detail=f"策略作者请求不得超过 {self._max_body_bytes} 字节",
                code="STRATEGY_AUTHORING_BODY_TOO_LARGE",
            )
            return
        if body is None:
            return

        field_error = _field_limit_error(body)
        if field_error is not None:
            field, limit = field_error
            await _reject(
                scope,
                receive,
                send,
                status_code=413,
                detail=f"字段 {field} 不得超过 {limit} 个字符",
                code="STRATEGY_AUTHORING_FIELD_TOO_LARGE",
            )
            return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        if not is_ai_strategy_request(path, method):
            await self.app(scope, replay_receive, send)
            return

        error_code, retry_after = self._limiter.acquire(_request_key(scope))
        if error_code is not None:
            detail = (
                "AI 策略生成任务繁忙,请稍后重试"
                if error_code == "STRATEGY_AI_BUSY"
                else "AI 策略请求过于频繁,请稍后重试"
            )
            await _reject(
                scope,
                replay_receive,
                send,
                status_code=429,
                detail=detail,
                code=error_code,
                retry_after=retry_after,
            )
            return

        try:
            await self.app(scope, replay_receive, send)
        finally:
            self._limiter.release()
