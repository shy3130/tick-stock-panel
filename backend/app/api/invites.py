"""Reusable invite-code access for the Sycee private beta."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.auth import _client_ip
from app.services import invites

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/invite", tags=["invite"])

_fail_counter: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
_fail_lock = Lock()
_MAX_FAILS = 8
_LOCK_SECONDS = 10 * 60


class InviteCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=128)


def _request_is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",")[0].strip() == "https"


def _check_rate_limit(ip: str) -> None:
    with _fail_lock:
        _, locked_until = _fail_counter.get(ip, (0, 0.0))
        now = time.time()
        if locked_until > now:
            raise HTTPException(
                status_code=429,
                detail=f"尝试次数过多,请 {int(locked_until - now)} 秒后重试",
            )
        if locked_until and locked_until <= now:
            _fail_counter.pop(ip, None)


def _record_failure(ip: str) -> None:
    with _fail_lock:
        if len(_fail_counter) > 1000:
            now = time.time()
            for stale in [key for key, (_, until) in _fail_counter.items() if until <= now]:
                _fail_counter.pop(stale, None)
        count, _ = _fail_counter.get(ip, (0, 0.0))
        count += 1
        locked_until = time.time() + _LOCK_SECONDS if count >= _MAX_FAILS else 0.0
        _fail_counter[ip] = (count, locked_until)


def _clear_failures(ip: str) -> None:
    with _fail_lock:
        _fail_counter.pop(ip, None)


@router.get("/status")
def invite_status(request: Request) -> dict:
    store = invites.get_store()
    token = request.cookies.get(invites.COOKIE_NAME)
    return {
        "enabled": store.enabled,
        "authorized": not store.enabled or store.is_valid_session(token),
        "capacity": store.capacity,
    }


@router.post("/redeem")
def redeem_invite(req: InviteCodeIn, request: Request, response: Response) -> dict:
    store = invites.get_store()
    if not store.enabled:
        return {"ok": True, "authorized": True}

    ip = _client_ip(request)
    _check_rate_limit(ip)
    session = store.redeem(req.code)
    if session is None:
        _record_failure(ip)
        raise HTTPException(status_code=401, detail="邀请码无效,请检查后重试")

    _clear_failures(ip)
    response.set_cookie(
        key=invites.COOKIE_NAME,
        value=session.token,
        max_age=invites.COOKIE_MAX_AGE,
        httponly=True,
        secure=_request_is_https(request),
        samesite="lax",
        path="/",
    )
    logger.info("private beta invite accepted from %s", ip)
    return {"ok": True, "authorized": True}
