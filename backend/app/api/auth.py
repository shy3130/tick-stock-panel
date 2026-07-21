"""Multi-user account authentication API."""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.services import users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "tf_session"
COOKIE_MAX_AGE = users.SESSION_TTL

_fail_counter: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
_fail_lock = Lock()
_MAX_FAILS = 5
_LOCK_SECONDS = 300


def _is_local_network(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    direct = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and _is_local_network(direct):
        return forwarded.split(",")[0].strip()
    return direct or "unknown"


def _request_is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",")[0].strip() == "https"


def _check_rate_limit(ip: str) -> None:
    with _fail_lock:
        _, until = _fail_counter.get(ip, (0, 0.0))
        now = time.time()
        if until > now:
            raise HTTPException(429, f"登录失败次数过多, 请 {int(until - now)} 秒后重试")
        if until:
            _fail_counter.pop(ip, None)


def _record_failure(ip: str) -> None:
    with _fail_lock:
        count, _ = _fail_counter.get(ip, (0, 0.0))
        count += 1
        _fail_counter[ip] = (count, time.time() + _LOCK_SECONDS if count >= _MAX_FAILS else 0.0)


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=_request_is_https(request),
        samesite="lax",
        path="/",
    )


class SetupIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


@router.get("/status")
def auth_status(request: Request) -> dict:
    user = users.user_for_session(settings.data_dir, request.cookies.get(COOKIE_NAME))
    return {
        "configured": users.is_configured(settings.data_dir),
        "authenticated": user is not None,
        "access_mode": "accounts",
        "invite_enabled": users.has_available_invites(settings.data_dir),
        "user": ({"id": user.id, "username": user.username, "role": user.role} if user else None),
    }


@router.post("/setup")
def setup_admin(req: SetupIn, request: Request, response: Response) -> dict:
    client_ip = _client_ip(request)
    if not _is_local_network(client_ip):
        raise HTTPException(403, "首次设置管理员仅允许本机或内网访问")
    if users.is_configured(settings.data_dir):
        raise HTTPException(409, "账户系统已初始化")
    user = users.create_user(
        settings.data_dir, "admin", req.password, role="admin", user_id="admin"
    )
    users.migrate_legacy_admin_data(settings.data_dir)
    _set_session_cookie(response, users.create_session(settings.data_dir, user), request)
    return {"ok": True, "authenticated": True, "user": user.__dict__}


@router.post("/login")
def login(req: LoginIn, request: Request, response: Response) -> dict:
    ip = _client_ip(request)
    _check_rate_limit(ip)
    user = users.authenticate(settings.data_dir, req.username, req.password)
    if not user:
        _record_failure(ip)
        raise HTTPException(401, "用户名或密码错误")
    with _fail_lock:
        _fail_counter.pop(ip, None)
    token = users.create_session(settings.data_dir, user)
    _set_session_cookie(response, token, request)
    return {"ok": True, "authenticated": True, "user": user.__dict__}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    users.revoke_session(settings.data_dir, request.cookies.get(COOKIE_NAME))
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(req: ChangePasswordIn, request: Request, response: Response) -> dict:
    current = users.user_for_session(settings.data_dir, request.cookies.get(COOKIE_NAME))
    if not current:
        raise HTTPException(401, "请先登录")
    if not users.authenticate(settings.data_dir, current.username, req.old_password):
        raise HTTPException(401, "旧密码错误")
    users.change_password(settings.data_dir, current.id, req.new_password)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True, "message": "密码已修改, 请重新登录"}
