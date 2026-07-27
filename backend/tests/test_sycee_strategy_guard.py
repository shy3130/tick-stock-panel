from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.main import app
from app.services.user_context import UserIdentity, as_user
from app.sycee.strategy_guard import (
    MAX_AUTHORING_BODY_BYTES,
    MAX_AUTHORING_FIELD_CHARS,
    StrategyAIRequestLimiter,
    SyceeStrategyAuthoringGuardMiddleware,
)


def _scope(
    path: str,
    *,
    method: str = "POST",
    content_length: int | None = None,
) -> dict:
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


async def _invoke(
    middleware,
    path: str,
    chunks: list[bytes],
    *,
    content_length: int | None = None,
    user: UserIdentity | None = None,
) -> tuple[int | None, dict[str, str], bytes]:
    incoming = deque(
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks or [b""])
    )
    sent: list[dict] = []

    async def receive() -> dict:
        if incoming:
            return incoming.popleft()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    with as_user(user):
        await middleware(
            _scope(path, content_length=content_length),
            receive,
            send,
        )

    start = next((message for message in sent if message["type"] == "http.response.start"), None)
    status = start["status"] if start else None
    headers = {
        name.decode().lower(): value.decode()
        for name, value in (start.get("headers", []) if start else [])
    }
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, headers, body


def _echo_app(received: list[bytes]):
    async def app(scope, receive, send) -> None:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        received.append(b"".join(chunks))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    return app


@pytest.mark.asyncio
async def test_rejects_declared_body_size_before_upstream_handler() -> None:
    received: list[bytes] = []
    guard = SyceeStrategyAuthoringGuardMiddleware(_echo_app(received))

    status, _, body = await _invoke(
        guard,
        "/api/strategies/code/save",
        [b""],
        content_length=MAX_AUTHORING_BODY_BYTES + 1,
    )

    assert status == 413
    assert json.loads(body)["code"] == "STRATEGY_AUTHORING_BODY_TOO_LARGE"
    assert received == []


@pytest.mark.asyncio
async def test_rejects_chunked_body_that_exceeds_limit_without_content_length() -> None:
    received: list[bytes] = []
    guard = SyceeStrategyAuthoringGuardMiddleware(_echo_app(received), max_body_bytes=8)

    status, _, body = await _invoke(
        guard,
        "/api/strategies/code/validate",
        [b"12345", b"6789"],
    )

    assert status == 413
    assert json.loads(body)["code"] == "STRATEGY_AUTHORING_BODY_TOO_LARGE"
    assert received == []


@pytest.mark.asyncio
async def test_rejects_oversized_authoring_field_below_total_body_limit() -> None:
    received: list[bytes] = []
    guard = SyceeStrategyAuthoringGuardMiddleware(_echo_app(received))
    body = json.dumps({"code": "x" * (MAX_AUTHORING_FIELD_CHARS["code"] + 1)}).encode()

    status, _, response_body = await _invoke(
        guard,
        "/api/strategies/code/save",
        [body],
        content_length=len(body),
    )

    assert status == 413
    payload = json.loads(response_body)
    assert payload["code"] == "STRATEGY_AUTHORING_FIELD_TOO_LARGE"
    assert "code" in payload["detail"]
    assert received == []


@pytest.mark.asyncio
async def test_replays_valid_body_and_bypasses_regular_strategy_configuration() -> None:
    received: list[bytes] = []
    guard = SyceeStrategyAuthoringGuardMiddleware(_echo_app(received), max_body_bytes=16)
    valid_authoring = b'{"code":"x"}'
    large_config = b"x" * 20

    authoring_status, _, _ = await _invoke(
        guard,
        "/api/strategies/code/validate",
        [valid_authoring],
        content_length=len(valid_authoring),
    )
    config_status, _, _ = await _invoke(
        guard,
        "/api/strategies/config",
        [large_config],
        content_length=len(large_config),
    )

    assert authoring_status == 200
    assert config_status == 200
    assert received == [valid_authoring, large_config]


def test_rate_limiter_is_per_user_and_resets_after_window() -> None:
    now = 0.0
    limiter = StrategyAIRequestLimiter(
        max_requests=2,
        window_seconds=60,
        max_concurrent=2,
        clock=lambda: now,
    )

    for _ in range(2):
        assert limiter.acquire("user:a") == (None, 0)
        limiter.release()
    assert limiter.acquire("user:a") == ("STRATEGY_AI_RATE_LIMIT", 60)
    assert limiter.acquire("user:b") == (None, 0)
    limiter.release()

    now = 60.0
    assert limiter.acquire("user:a") == (None, 0)
    limiter.release()


@pytest.mark.asyncio
async def test_ai_rate_limit_isolated_by_authenticated_user() -> None:
    received: list[bytes] = []
    limiter = StrategyAIRequestLimiter(max_requests=1, window_seconds=60, max_concurrent=2)
    guard = SyceeStrategyAuthoringGuardMiddleware(_echo_app(received), limiter=limiter)
    alice = UserIdentity("alice-id", "alice", "admin")
    bob = UserIdentity("bob-id", "bob", "admin")

    alice_first, _, _ = await _invoke(
        guard,
        "/api/strategies/ai/test",
        [b""],
        user=alice,
    )
    alice_second, headers, body = await _invoke(
        guard,
        "/api/strategies/ai/test",
        [b""],
        user=alice,
    )
    bob_first, _, _ = await _invoke(
        guard,
        "/api/strategies/ai/test",
        [b""],
        user=bob,
    )

    assert alice_first == 200
    assert alice_second == 429
    assert headers["retry-after"] == "60"
    assert json.loads(body)["code"] == "STRATEGY_AI_RATE_LIMIT"
    assert bob_first == 200


@pytest.mark.asyncio
async def test_concurrency_slot_is_held_until_streaming_response_finishes() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    async def streaming_app(scope, receive, send) -> None:
        await receive()
        started.set()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await finish.wait()
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    limiter = StrategyAIRequestLimiter(max_requests=10, max_concurrent=1)
    guard = SyceeStrategyAuthoringGuardMiddleware(streaming_app, limiter=limiter)
    admin = UserIdentity("admin-id", "admin", "admin")

    first = asyncio.create_task(
        _invoke(guard, "/api/strategies/build/stream", [b"{}"], user=admin)
    )
    await started.wait()
    second_status, second_headers, second_body = await _invoke(
        guard,
        "/api/strategies/build/stream",
        [b"{}"],
        user=admin,
    )

    assert second_status == 429
    assert second_headers["retry-after"] == "1"
    assert json.loads(second_body)["code"] == "STRATEGY_AI_BUSY"

    finish.set()
    first_status, _, first_body = await first
    assert first_status == 200
    assert first_body == b"firstlast"


def test_guard_is_inside_auth_and_cors_middleware() -> None:
    middleware_classes = [entry.cls for entry in app.user_middleware]

    assert middleware_classes.index(BaseHTTPMiddleware) < middleware_classes.index(CORSMiddleware)
    assert middleware_classes.index(CORSMiddleware) < middleware_classes.index(
        SyceeStrategyAuthoringGuardMiddleware
    )


@pytest.mark.asyncio
async def test_complete_app_stack_replays_body_and_returns_guard_errors(monkeypatch) -> None:
    from app.services import users

    monkeypatch.setattr(
        users,
        "user_for_session",
        lambda *_: UserIdentity("admin-id", "admin", "admin"),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        validate = await client.post(
            "/api/strategies/code/validate",
            json={"code": "not valid python", "strategy_id": "custom_stack_probe"},
        )
        oversized = await client.post(
            "/api/strategies/code/validate",
            json={
                "code": "x" * (MAX_AUTHORING_FIELD_CHARS["code"] + 1),
                "strategy_id": "custom_stack_probe",
            },
            headers={"Origin": "http://example.test"},
        )

    assert validate.status_code == 200
    assert validate.json()["valid"] is False
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "STRATEGY_AUTHORING_FIELD_TOO_LARGE"
    assert oversized.headers["access-control-allow-origin"] == "*"
