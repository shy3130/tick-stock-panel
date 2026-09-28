"""SSE 短期票据 — EventSource 无法携带 Authorization 头的替代通道。

流程 (docs/open-platform-plan.md §6 V2):
  1. POST /api/events/ticket (Bearer Token, 任意 scope) → 一次性票据 (tse_ 前缀)
  2. GET /api/events?ticket=... (EventSource) → 校验+消费票据 → 按票据
     继承的 scope 过滤事件流
  3. 票据 60 秒过期 / 消费即作废; 重连需重新走第 1 步换新票据

安全边界: 票据只继承 Token 已有 scope, 不放大权限; 存活窗口短,
即使泄漏也只能订阅事件流, 不能调用任何 REST 端点。
进程内存储 (单进程 uvicorn 场景), 重启清零无害。
"""
from __future__ import annotations

import secrets
import threading
import time

TICKET_PREFIX = "tse_"
TTL_S = 60.0
_MAX_OUTSTANDING = 1000  # 防堆积: 远超正常并发, 超限先清理过期

_LOCK = threading.Lock()
_TICKETS: dict[str, tuple[float, list[str]]] = {}  # ticket → (过期时刻, scopes)


def _prune(now: float) -> None:
    expired = [t for t, (exp, _) in _TICKETS.items() if exp <= now]
    for t in expired:
        _TICKETS.pop(t, None)


def issue(scopes: list[str]) -> tuple[str, float]:
    """签发一次性票据; 返回 (票据, 有效期秒)。"""
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        if len(_TICKETS) >= _MAX_OUTSTANDING:
            raise RuntimeError("待使用票据过多, 稍后重试")
        ticket = TICKET_PREFIX + secrets.token_hex(16)
        _TICKETS[ticket] = (now + TTL_S, list(scopes))
    return ticket, TTL_S


def consume(ticket: str) -> list[str] | None:
    """校验并消费票据; 返回继承的 scopes, 无效/过期/已用 → None。"""
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        hit = _TICKETS.pop(ticket, None)
    if hit is None or hit[0] <= now:
        return None
    return hit[1]
