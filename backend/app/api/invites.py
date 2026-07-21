"""Invite-code registration for multi-user accounts."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.auth import COOKIE_NAME, _client_ip, _request_is_https
from app.config import settings
from app.services import invites as legacy_invites
from app.services import users

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/invite", tags=["invite"])

_fail_counter: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
_fail_lock = Lock()
_MAX_FAILS = 8
_LOCK_SECONDS = 10 * 60


class RegisterIn(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    username: str | None = Field(default=None, min_length=2, max_length=64)
    password: str | None = Field(default=None, min_length=6, max_length=128)


def _check_rate_limit(ip: str) -> None:
    with _fail_lock:
        _, until = _fail_counter.get(ip, (0, 0.0))
        if until > time.time():
            raise HTTPException(429, f"尝试次数过多,请 {int(until - time.time())} 秒后重试")
        if until:
            _fail_counter.pop(ip, None)


def _record_failure(ip: str) -> None:
    with _fail_lock:
        count, _ = _fail_counter.get(ip, (0, 0.0))
        count += 1
        _fail_counter[ip] = (count, time.time() + _LOCK_SECONDS if count >= _MAX_FAILS else 0.0)


@router.get("/status")
def invite_status(request: Request) -> dict:
    users.sync_env_invites(settings.data_dir, settings.invite_codes)
    current = users.user_for_session(settings.data_dir, request.cookies.get(COOKIE_NAME))
    invites = users.list_invites(settings.data_dir)
    legacy = legacy_invites.get_store()
    if not settings.invite_codes.strip() and legacy.enabled and not invites:
        return {
            "enabled": True,
            "authorized": legacy.is_valid_session(request.cookies.get(legacy_invites.COOKIE_NAME)),
            "capacity": legacy.capacity,
        }
    return {
        "enabled": users.has_invites(settings.data_dir),
        "authorized": current is not None,
        "capacity": len(invites),
        "available": sum(1 for item in invites if not item["redeemed_by"]),
    }


@router.post("/redeem")
def register(req: RegisterIn, request: Request, response: Response) -> dict:
    if not req.username or not req.password:
        legacy = legacy_invites.get_store()
        if not settings.invite_codes.strip() and legacy.enabled:
            session = legacy.redeem(req.code)
            if session is None:
                raise HTTPException(401, "邀请码无效,请检查后重试")
            response.set_cookie(
                key=legacy_invites.COOKIE_NAME,
                value=session.token,
                max_age=legacy_invites.COOKIE_MAX_AGE,
                httponly=True,
                secure=_request_is_https(request),
                samesite="lax",
                path="/",
            )
            return {"ok": True, "authorized": True}
        raise HTTPException(422, "请填写用户名和密码")
    users.sync_env_invites(settings.data_dir, settings.invite_codes)
    ip = _client_ip(request)
    _check_rate_limit(ip)
    try:
        user = users.register_with_invite(settings.data_dir, req.code, req.username, req.password)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if user is None:
        _record_failure(ip)
        raise HTTPException(401, "邀请码无效或已使用")
    token = users.create_session(settings.data_dir, user)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=users.SESSION_TTL,
        httponly=True,
        secure=_request_is_https(request),
        samesite="lax",
        path="/",
    )
    with _fail_lock:
        _fail_counter.pop(ip, None)
    logger.info("user %s registered from %s", user.username, ip)
    return {"ok": True, "authorized": True, "user": user.__dict__}
