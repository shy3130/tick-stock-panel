"""访问认证 API。

端点:
  GET  /api/auth/status        — 是否已设密码、当前会话是否有效
  POST /api/auth/setup         — 首次设置密码(仅限本机/内网, 防公网抢占)
  POST /api/auth/login         — 登录(密码 → 会话 token, 含限流)
  POST /api/auth/logout        — 注销当前会话
  POST /api/auth/change-password — 改密码(需已登录)

安全:
  - setup 端点只接受本机/内网请求(request.client.host), 公网请求 403。
    否则黑客可比用户更早扫到域名, 抢先设密码, 反客为主。
  - login 限流: 同一来源 IP 连续失败 5 次, 锁 5 分钟(内存计数)。
  - 会话 token 通过 HttpOnly cookie 下发, 前端无需手动管理。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.services import auth
from app.services.api_gateway import is_local_network as _is_local_network
from app.services.api_gateway import is_loopback as _is_loopback

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "tf_session"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 与 SESSION_TTL 一致

# 限流: { ip: (fail_count, lock_until_ts) }
_fail_counter: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
_fail_lock = Lock()
_MAX_FAILS = 5
_LOCK_SECONDS = 300


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。

    安全关键: 仅当直连 peer(request.client.host)本身是回环/内网地址
    (即请求确实经过同机/内网的可信反代)时, 才采信 X-Forwarded-For。
    反代会把真实对端 IP 追加在 X-Forwarded-For 最右侧, 必须从右向左解析:
    先取首个非内网 IP(防公网攻击者在最左端伪造 127.0.0.1 绕过门禁或轮换 IP
    绕过登录限流); 若全为内网 IP, 取首个非回环内网 IP(防局域网客户端冒充本机回环)。
    """
    direct = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    if xff and direct and _is_local_network(direct):
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            for hop in reversed(hops):
                if not _is_local_network(hop):
                    return hop
            for hop in reversed(hops):
                if not _is_loopback(hop):
                    return hop
            return hops[-1]
    return direct or "unknown"


def is_cross_origin_request(
    request: Request,
    allowed_origins: tuple[str, ...] | list[str] = (),
) -> bool:
    """判断浏览器请求是否来自非同源/跨站上下文。"""
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        if origin in allowed_origins:
            return False
        if urlsplit(origin).netloc != (request.headers.get("host") or ""):
            return True
    if request.headers.get("sec-fetch-site") == "cross-site":
        return not (origin and origin in allowed_origins)
    return False


def require_management(request: Request) -> None:
    """高危管理操作统一门禁 (代码/插件写入、密钥导出、备份恢复、Token 管理、目录删除)。

    与只读/业务接口不同, 高危操作不接受 API Token, 拒绝跨站来源,
    且未设密码时仅允许本机回环同源访问 (局域网须先设密码并登录)。
    """
    if request.headers.get("authorization"):
        raise HTTPException(403, "管理操作仅供面板管理会话使用")
    origin = (request.headers.get("origin") or "").strip()
    if origin and urlsplit(origin).netloc != (request.headers.get("host") or ""):
        raise HTTPException(403, "管理操作需要同源访问")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "拒绝跨站管理请求")
    if auth.is_configured():
        if not auth.is_valid_session(request.cookies.get(COOKIE_NAME, "")):
            raise HTTPException(401, "请先登录")
    elif not _is_loopback(_client_ip(request)) or not _is_loopback(request.url.hostname):
        raise HTTPException(403, "请先设置访问密码并登录，或通过本机访问此管理功能")


def _check_login_rate_limit(ip: str) -> None:
    """登录失败限流检查, 触发则抛 429。锁定过期后重置计数(重新给 5 次机会)。"""
    with _fail_lock:
        count, until = _fail_counter.get(ip, (0, 0.0))
        now = time.time()
        if until > now:
            wait = int(until - now)
            raise HTTPException(
                status_code=429,
                detail=f"登录失败次数过多, 请 {wait} 秒后重试",
            )
        if until and until <= now:
            # 锁定已过期: 清除旧计数, 否则之后每失败一次都会立刻再锁 5 分钟
            _fail_counter.pop(ip, None)


def _record_login_fail(ip: str) -> None:
    """记录一次登录失败, 达阈值则锁定。"""
    with _fail_lock:
        # 防内存膨胀: 条目过多时清掉已过锁定期的记录
        if len(_fail_counter) > 1000:
            now = time.time()
            for stale in [k for k, (_, u) in _fail_counter.items() if u <= now]:
                _fail_counter.pop(stale, None)
        count, until = _fail_counter.get(ip, (0, 0.0))
        count += 1
        if count >= _MAX_FAILS:
            until = time.time() + _LOCK_SECONDS
            logger.warning("auth login locked for %s after %d fails", ip, count)
        _fail_counter[ip] = (count, until)


def _clear_login_fails(ip: str) -> None:
    """登录成功后清除该 IP 的失败计数。"""
    with _fail_lock:
        _fail_counter.pop(ip, None)


# ================================================================
# 端点
# ================================================================

class PasswordIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


@router.get("/status")
def auth_status(request: Request) -> dict:
    """认证状态: 是否已设密码 + 当前请求是否已登录。"""
    token = request.cookies.get(COOKIE_NAME)
    return {
        "configured": auth.is_configured(),
        "authenticated": bool(token and auth.is_valid_session(token)),
    }


@router.post("/setup")
def setup_password(req: PasswordIn, request: Request) -> dict:
    """首次设置访问密码。仅限本机/内网同源请求(防公网抢占与跨站劫持)。

    若已设置过密码, 返回 409(改密码走 /change-password)。
    """
    if is_cross_origin_request(request):
        raise HTTPException(status_code=403, detail="首次设置密码需要同源访问")
    # 关键: 限制只有服务器主人(本机/内网)能设密码
    client_ip = _client_ip(request)
    if not _is_local_network(client_ip):
        logger.warning("setup rejected from non-local ip: %s", client_ip)
        raise HTTPException(
            status_code=403,
            detail="首次设置密码仅允许本机或内网访问,请通过 SSH/本地浏览器操作",
        )

    if auth.is_configured():
        raise HTTPException(status_code=409, detail="密码已设置,如需修改请登录后使用改密码功能")

    auth.set_password(req.password)
    logger.info("access password set up from %s", client_ip)
    return {"ok": True, "configured": True}


@router.post("/login")
def login(req: LoginIn, request: Request, response: Response) -> dict:
    """登录: 密码 → 会话 token(写 HttpOnly cookie)。含失败限流。"""
    ip = _client_ip(request)
    _check_login_rate_limit(ip)

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="尚未设置访问密码")

    token = auth.verify_and_create_session(req.password)
    if not token:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="密码错误")

    _clear_login_fails(ip)
    # HttpOnly: 防 XSS 窃取; SameSite=Lax: 防 CSRF; Path=/: 全站生效
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,  # 自托管可能无 HTTPS, 不强制 secure(建议反代加 HTTPS)
    )
    return {"ok": True, "authenticated": True}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """注销当前会话。"""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(req: ChangePasswordIn, request: Request) -> dict:
    """修改密码: 需验证旧密码, 成功后所有会话失效(含当前, 需重新登录)。"""
    token = request.cookies.get(COOKIE_NAME)
    if not (token and auth.is_valid_session(token)):
        raise HTTPException(status_code=401, detail="请先登录")

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="尚未设置访问密码")

    ip = _client_ip(request)
    _check_login_rate_limit(ip)

    # 验证旧密码
    new_token = auth.verify_and_create_session(req.old_password)
    if not new_token:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="旧密码错误")
    # 临时 token 用完即弃
    auth.revoke_session(new_token)
    _clear_login_fails(ip)

    # 改密码(set_password 会清空所有会话)
    auth.set_password(req.new_password)
    return {"ok": True, "message": "密码已修改, 请重新登录"}
