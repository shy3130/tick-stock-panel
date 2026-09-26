"""开放接口示例脚本 — 公共工具 (纯标准库, 无第三方依赖)。

使用前:
  1. 面板 → 设置 → 开放接口 → 新建 Token (按需勾选 scope)
  2. 设置环境变量:
       TSP_TOKEN=tsp_xxx            (必填)
       TSP_BASE=http://127.0.0.1:3018  (默认本机开发端口, 按实际改)
  3. 逐个运行: python 01_pull_market.py
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("TSP_BASE", "http://127.0.0.1:3018").rstrip("/")
TOKEN = os.environ.get("TSP_TOKEN", "")


def call(method: str, path: str, body: dict | None = None, headers: dict[str, str] | None = None):
    """调开放接口; 非 2xx 抛 RuntimeError 并带出服务端 detail。"""
    if not TOKEN:
        raise SystemExit("请先设置环境变量 TSP_TOKEN (设置 → 开放接口 → 新建 Token)")
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from e


def sse(path: str):
    """GET 一个 SSE 流, 逐行 yield (demo 用; 生产建议浏览器 EventSource)。"""
    req = urllib.request.Request(BASE + path, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=None) as resp:
        event: dict[str, str] = {}
        for raw in resp:
            line = raw.decode().rstrip("\n")
            if not line:
                if event:
                    yield event
                    event = {}
                continue
            if line.startswith(":"):
                continue  # 心跳注释帧
            key, _, val = line.partition(": ")
            event[key] = val
