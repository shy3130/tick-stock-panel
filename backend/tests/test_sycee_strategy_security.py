from __future__ import annotations

import pytest
from fastapi import Request, Response

from app.main import _admin_required, auth_middleware
from app.services.user_context import UserIdentity
from app.sycee.strategy_security import strategy_authoring_requires_admin


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/strategies/ai/generate", "POST"),
        ("/api/strategies/ai/save", "POST"),
        ("/api/strategies/ai/test", "POST"),
        ("/api/strategies/build", "POST"),
        ("/api/strategies/build/stream", "POST"),
        ("/api/strategies/code/save", "POST"),
        ("/api/strategies/code/validate", "POST"),
        ("/api/strategies/reload", "POST"),
        ("/api/strategies/custom_example", "DELETE"),
    ],
)
def test_strategy_authoring_requires_admin(path: str, method: str) -> None:
    assert strategy_authoring_requires_admin(path, method)
    assert _admin_required(path, method)


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/strategies", "GET"),
        ("/api/strategies/custom_example", "GET"),
        ("/api/strategies/run", "POST"),
        ("/api/strategies/run-all", "POST"),
        ("/api/strategies/config", "POST"),
        ("/api/strategies/config/custom_example", "DELETE"),
    ],
)
def test_strategy_use_and_parameter_configuration_stay_available(path: str, method: str) -> None:
    assert not strategy_authoring_requires_admin(path, method)
    assert not _admin_required(path, method)


def _request(path: str, method: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


@pytest.mark.asyncio
async def test_member_is_blocked_before_strategy_authoring_handler(monkeypatch) -> None:
    from app.services import users

    monkeypatch.setattr(
        users,
        "user_for_session",
        lambda *_: UserIdentity("member-id", "member", "user"),
    )
    called = False

    async def call_next(_request: Request) -> Response:
        nonlocal called
        called = True
        return Response(status_code=204)

    response = await auth_middleware(
        _request("/api/strategies/code/save", "POST"),
        call_next,
    )

    assert response.status_code == 403
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "path", "method"),
    [
        ("admin", "/api/strategies/code/save", "POST"),
        ("user", "/api/strategies/config", "POST"),
    ],
)
async def test_authorized_strategy_requests_reach_handler(
    monkeypatch,
    role: str,
    path: str,
    method: str,
) -> None:
    from app.services import users

    monkeypatch.setattr(
        users,
        "user_for_session",
        lambda *_: UserIdentity(f"{role}-id", role, role),
    )
    called = False

    async def call_next(_request: Request) -> Response:
        nonlocal called
        called = True
        return Response(status_code=204)

    response = await auth_middleware(_request(path, method), call_next)

    assert response.status_code == 204
    assert called
