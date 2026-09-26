"""MCP 服务器冒烟测试 — 子进程拉起, 走完整 JSON-RPC: 握手/工具清单/实际调用。

用法: TSP_TOKEN=tsp_xxx [TSP_BASE=...] python smoke_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "tsp_mcp_server.server"],
        cwd=os.path.join(BASE_DIR, "src"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=os.environ,
    )
    next_id = 0

    def send(method: str, params: dict | None = None, *, notify: bool = False) -> dict | None:
        nonlocal next_id
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            next_id += 1
            msg["id"] = next_id
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if notify:
            return None
        # 读到带对应 id 的响应 (跳过日志/通知行)
        while True:
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read()
                raise RuntimeError(f"MCP server 提前退出: {err[-500:]}")
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == next_id:
                if "error" in resp:
                    raise RuntimeError(f"JSON-RPC 错误: {resp['error']}")
                return resp["result"]

    try:
        # 1. 握手
        init = send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        })
        print("server:", init["serverInfo"]["name"])
        send("notifications/initialized", notify=True)

        # 2. 工具清单
        tools = send("tools/list")["tools"]
        print(f"tools ({len(tools)}):", ", ".join(sorted(t["name"] for t in tools)))
        names = {t["name"] for t in tools}

        # 3. 实际调用: 搜索 + 总览 + 环境
        calls = [
            ("search_instrument", {"query": "600519", "limit": 2}, "贵州茅台"),
            ("get_market_overview", {}, "as_of"),
            ("get_regime_state", {}, "row"),
        ]
        for name, args, expect in calls:
            if name not in names:
                print(f"skip {name} (scope 未开放)")
                continue
            r = send("tools/call", {"name": name, "arguments": args})
            text = r["content"][0]["text"]
            assert expect in text, f"{name} 返回异常: {text[:200]}"
            print(f"{name}: OK -> {text[:100]}")
        print("SMOKE PASS")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
