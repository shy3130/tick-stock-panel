"""通过面板 MCP 查标的, 再用匿名 Parallel Search MCP 获取外部研究来源。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

PARALLEL_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "tick-stock-panel-mcp-research/0.3.2"


def decode_result(result) -> dict:
    if result.is_error:
        raise RuntimeError("MCP 工具调用失败")
    # MCPServer 包装 str 返回值为 {"result": str}; 原文 JSON 在 text 内容中。
    if result.structured_content is not None and set(result.structured_content) != {"result"}:
        return result.structured_content
    text = "\n".join(c.text for c in result.content if c.type == "text")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RuntimeError("MCP 工具未返回有效 JSON, 请检查面板 Token 和权限") from exc
    if not isinstance(data, dict):
        raise TypeError("MCP 工具返回结构不正确")
    return data


async def lookup_instrument(symbol: str) -> dict:
    if not os.environ.get("TSP_TOKEN", "").strip():
        raise RuntimeError("未设置 TSP_TOKEN, 请创建 read:market Token")
    # 只把面板凭据交给本地 stdio 子进程; 不交给外部搜索服务。
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tsp_mcp_server.server"],
        cwd=str(Path(__file__).resolve().parents[1] / "src"),
        env={**os.environ, "TSP_SCOPES": "read:market"},
    )
    async with (
        stdio_client(server) as (read, write),
        ClientSession(read, write, read_timeout_seconds=60) as session,
    ):
        await session.initialize()
        raw = await session.call_tool(
            "search_instrument", {"query": symbol, "limit": 8},
        )
    data = decode_result(raw)
    matches = [row for row in data.get("results", []) if row.get("symbol") == symbol]
    if len(matches) != 1 or not matches[0].get("name"):
        raise RuntimeError("面板未找到唯一的完整标的代码, 请先同步标的列表")
    return {"symbol": symbol, "name": matches[0]["name"]}


async def search_web(instrument: dict, topic: str, url: str | None) -> dict:
    query = f"{instrument['name']} {instrument['symbol']} {topic}"
    session_id = uuid.uuid4().hex
    # 新建无认证客户端, 不读取 Parallel Key 或面板 Token。
    async with (
        httpx2.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=60,
        ) as http,
        streamable_http_client(PARALLEL_URL, http_client=http) as (read, write),
        ClientSession(read, write, read_timeout_seconds=60) as session,
    ):
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        required = {"web_search", "web_fetch"} if url else {"web_search"}
        if not required <= tools:
            raise RuntimeError("Parallel MCP 缺少研究工具")
        result = {"instrument": instrument, "search": decode_result(await session.call_tool(
            "web_search", {"objective": query, "search_queries": [query], "session_id": session_id},
        ))}
        if url:
            result["fetch"] = decode_result(await session.call_tool(
                "web_fetch", {"urls": [url], "session_id": session_id},
            ))
        return result


async def research(symbol: str, topic: str, url: str | None) -> dict:
    async with asyncio.timeout(180):
        instrument = await lookup_instrument(symbol)
        return await search_web(instrument, topic, url)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", help="完整 A 股标的代码, 如 600519.SH")
    parser.add_argument("--topic", default="最新公司公告", help="公开资料搜索主题")
    parser.add_argument("--url", help="需要额外读取的公开网页 HTTP(S) 地址")
    args = parser.parse_args()
    args.symbol = args.symbol.upper()
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", args.symbol):
        parser.error("请使用完整标的代码, 如 600519.SH")
    if not args.topic.strip() or len(args.topic) > 200:
        parser.error("主题须为 1 到 200 字")
    if args.url and not re.match(r"https?://[^/\s]+", args.url):
        parser.error("--url 须为 HTTP(S) 地址")
    try:
        result = asyncio.run(research(args.symbol, args.topic, args.url))
    except Exception:  # noqa: BLE001 — CLI 边界必须避免打印凭据, 包括 transport ExceptionGroup。
        # 网络异常可能携带请求信息; 不输出 Token 或配置。
        print("研究失败: 请检查面板连接、TSP_TOKEN/read:market 权限与 MCP 服务可用性。", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
