"""API 开放网关 — Token 请求的 scope 校验与限流 (docs/open-platform-plan.md §4-5)。

evaluate() 是唯一入口: 认证中间件把 Bearer 明文交给它, 得到
放行 / 401 / 403 / 429 的裁决与响应头。UI 会话完全不经过本模块。

规则表按顺序匹配 (方法 + 路径前缀); 未命中 = 不对外开放 (403)。
刻意不放进规则表的: SSE 流端点 (EventSource 无法带 Authorization 头,
V2 用短期票据解决)、watchlist/监控规则管理、数据同步、设置 —— 保持最小开放面。
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

from app.services import api_tokens

# (method, 路径前缀, scope) — method 用 "*" 表示任意; 顺序敏感, 具体规则在前
_RULES: list[tuple[str, str, str]] = [
    # read:market — 行情读取
    ("GET", "/api/kline/instruments/search", "read:market"),
    ("GET", "/api/kline/daily", "read:market"),
    ("GET", "/api/kline/minute", "read:market"),
    ("GET", "/api/kline/minute-range", "read:market"),
    ("GET", "/api/intraday/status", "read:market"),
    ("GET", "/api/intraday/indices", "read:market"),
    ("GET", "/api/index/daily", "read:market"),
    ("GET", "/api/index/minute", "read:market"),
    ("GET", "/api/overview/market", "read:market"),
    ("GET", "/api/screener/market-snapshot", "read:market"),
    # read:ext — 扩展数据读取 (Key 状态子路径除外, 属管理面)
    ("GET", "/api/ext-data/schema-all", "read:ext"),
    ("GET", "/api/ext-data/schema/", "read:ext"),
    ("GET", "/api/ext-data/", "read:ext"),
    ("GET", "/api/ext-data", "read:ext"),
    # read:analysis — 策略/回测结果/环境/告警 读取
    ("GET", "/api/strategies", "read:analysis"),
    ("GET", "/api/screener/strategies", "read:analysis"),
    ("GET", "/api/screener/cached", "read:analysis"),
    ("GET", "/api/screener/limit-ladder", "read:analysis"),
    ("GET", "/api/backtest/status", "read:analysis"),
    ("GET", "/api/backtest/candidates", "read:analysis"),
    ("GET", "/api/backtest/factor/columns", "read:analysis"),
    ("GET", "/api/regime", "read:analysis"),
    ("GET", "/api/alerts", "read:analysis"),
    # run:backtest — 触发计算任务
    ("POST", "/api/screener/run", "run:backtest"),
    ("POST", "/api/screener/run_preset", "run:backtest"),
    ("POST", "/api/screener/run_all", "run:backtest"),
    ("POST", "/api/backtest/run", "run:backtest"),
    ("POST", "/api/backtest/factor/run", "run:backtest"),
    ("POST", "/api/backtest/factor/batch", "run:backtest"),
    ("POST", "/api/backtest/strategy/run", "run:backtest"),
    # paper:trade — 模拟盘全部 (读+写一体, 单一 scope 简化心智)
    ("*", "/api/paper", "paper:trade"),
]

_RATE_WINDOW_S = 60.0


def rate_limit_per_min() -> int:
    """每 Token 每分钟上限 (env OPEN_API_RATE_LIMIT_PER_MIN, 默认 120, 范围 1~10000)。"""
    try:
        v = int(os.environ.get("OPEN_API_RATE_LIMIT_PER_MIN", "120"))
        return v if 1 <= v <= 10000 else 120
    except ValueError:
        return 120


def required_scope(method: str, path: str) -> str | None:
    """路径+方法 → 所需 scope; None = 该端点不对外开放。"""
    for m, prefix, scope in _RULES:
        if (m == "*" or m == method) and (path == prefix or path.startswith(prefix.rstrip("/") + "/")):
            # read:ext 域内的拉取 Key 状态子路径属管理面 (脱敏也不外露)
            if scope == "read:ext" and path.endswith("/api-key"):
                return None
            return scope
    return None


# ── 限流: 进程内滑动窗口 (每 token_id 一个 deque; 重启清零无害) ──
_BUCKETS: dict[str, deque[float]] = {}
_BUCKET_LOCK = threading.Lock()


def _allow(key: str, limit: int) -> tuple[bool, int, int]:
    """返回 (是否放行, 剩余额度, 重试等待秒)。"""
    now = time.monotonic()
    with _BUCKET_LOCK:
        q = _BUCKETS.setdefault(key, deque())
        while q and now - q[0] >= _RATE_WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            retry = max(1, int(_RATE_WINDOW_S - (now - q[0])) + 1)
            return False, 0, retry
        q.append(now)
        return True, limit - len(q), 0


def evaluate(data_dir: Path, method: str, path: str, plaintext: str) -> dict:
    """Token 请求裁决: {status: None=放行 | 401/403/429, detail, headers}。"""
    record = api_tokens.verify_token(data_dir, plaintext)
    if record is None:
        return {"status": 401, "detail": "API Token 无效或已吊销", "headers": {}}

    scope = required_scope(method, path)
    if scope is None:
        return {
            "status": 403,
            "detail": f"该端点 ({method} {path}) 未对外开放; 开放清单见 /api/openapi.json?tier=a",
            "headers": {},
        }
    if scope not in record.get("scopes", []):
        return {
            "status": 403,
            "detail": f"Token 缺少所需 scope: {scope} (持有: {record.get('scopes', [])})",
            "headers": {},
        }

    limit = rate_limit_per_min()
    ok, remaining, retry = _allow(record["id"], limit)
    if not ok:
        return {
            "status": 429,
            "detail": f"请求超出限额 ({limit}/分钟), {retry} 秒后重试",
            "headers": {"Retry-After": str(retry)},
        }
    return {
        "status": None,
        "detail": "",
        "headers": {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
        },
    }
