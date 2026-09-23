"""Wind 万得行情代理 API。

把前端 Wind 行情面板的需求透传到 Wind MCP skill 的 node CLI
（~/.agents/skills/wind-mcp-skill/scripts/cli.mjs），并把返回的 data.{columns,rows}
原样交给前端渲染。后端不缓存、不重试、不批量拉取——只在被调用时发起单次请求，
避免消耗 Wind 配额（token / 积分）。

端点
----
  GET /api/wind/status            -> 仅探测 node + cli.mjs 是否就绪（不联网）
  GET /api/wind/quote             -> 个股行情快照 (get_stock_price_indicators)
  GET /api/wind/kline             -> K 线 (get_stock_kline)
  GET /api/wind/search            -> 股票筛选 (search_stocks)
  POST /api/wind/call             -> 通用透传 (任意 server_type + tool + params)

错误统一以 {"ok": false, "message": "..."} 返回（HTTP 200），便于前端按业务错误展示，
而不是让全局拦截器当成 5xx。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/wind", tags=["wind"])

_DEFAULT_CLI_DIR = os.path.expanduser("~/.agents/skills/wind-mcp-skill")
CLI_DIR = os.environ.get("WIND_CLI_DIR", _DEFAULT_CLI_DIR)
CLI_JS = os.path.join(CLI_DIR, "scripts", "cli.mjs")
_TIMEOUT = float(os.environ.get("WIND_CLI_TIMEOUT", "30"))


def _node() -> Optional[str]:
    return shutil.which("node")


def _status() -> Dict[str, Any]:
    return {
        "ok": True,
        "node_present": _node() is not None,
        "cli_present": os.path.exists(CLI_JS),
        "cli_dir": CLI_DIR,
    }


def _call_wind(server_type: str, tool: str,
               params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """调用 cli.mjs 并返回内层 payload dict。失败时返回 {"ok": false, ...}。"""
    node = _node()
    if node is None:
        return {"ok": False, "message": "未找到 node 可执行文件（请安装 Node.js 并加入 PATH）。"}
    if not os.path.exists(CLI_JS):
        return {"ok": False, "message": f"Wind CLI 不存在：{CLI_JS}。请先安装 wind-mcp-skill。"}

    payload = json.dumps(params or {}, ensure_ascii=False)
    cmd = [node, CLI_JS, "call", server_type, tool, payload]
    try:
        proc = subprocess.run(
            cmd, cwd=CLI_DIR, capture_output=True, text=True,
            timeout=_TIMEOUT, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"Wind CLI 超时（>{_TIMEOUT}s）：{server_type}.{tool}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"调用 Wind CLI 失败：{exc}"}

    if proc.returncode != 0:
        return {"ok": False, "message": f"Wind CLI 退出码 {proc.returncode}：{proc.stderr.strip()[:400]}"}

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "message": f"Wind CLI 输出非 JSON：{proc.stdout[:400]}"}

    if isinstance(envelope, dict) and envelope.get("ok") is False:
        return {"ok": False, "code": envelope.get("code"),
                "message": envelope.get("message", "Wind 返回错误")}

    text = ""
    if isinstance(envelope, dict):
        for part in envelope.get("content", []) or []:
            if isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")
    if not text:
        return {"ok": True, "data": {}}
    try:
        inner = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "message": f"Wind 内层响应非 JSON：{text[:400]}"}
    return {"ok": True, "data": inner}


@router.get("/status")
def wind_status() -> Dict[str, Any]:
    return _status()


@router.get("/quote")
def wind_quote(
    windcode: str = Query(..., description="Wind 代码，如 600519.SH；多个逗号分隔，单次最多 50"),
    indexes: Optional[str] = Query(
        None,
        description="逗号分隔的中文指标名（逐字取自 Wind 指标集）；省略走默认指标集",
    ),
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"windcode": windcode}
    if indexes:
        params["indexes"] = indexes
    return _call_wind("stock_data", "get_stock_price_indicators", params)


@router.get("/kline")
def wind_kline(
    windcode: str = Query(..., description="Wind 代码，如 600519.SH"),
    begin_date: str = Query(..., description="开始日期 yyyy-MM-dd"),
    end_date: str = Query(..., description="结束日期 yyyy-MM-dd"),
    period: str = Query("1d", description="K 线周期：1d/1w/1mo/1y/1q/5min..."),
    aftype: str = Query("0", description="复权类型：0=前复权 1=后复权 2=不复权"),
) -> Dict[str, Any]:
    params = {
        "windcode": windcode,
        "begin_date": begin_date,
        "end_date": end_date,
        "period": period,
        "aftype": aftype,
    }
    return _call_wind("stock_data", "get_stock_kline", params)


@router.get("/search")
def wind_search(
    question: str = Query(..., description="自然语言筛选条件，如 沪深市场市值超500亿且连续5日上涨"),
) -> Dict[str, Any]:
    return _call_wind("stock_data", "search_stocks", {"question": question})


@router.post("/call")
async def wind_call(request: Request) -> Dict[str, Any]:
    """通用透传：任意 server_type + tool + params。

    请求体：{"server_type": "stock_data", "tool": "get_stock_basicinfo", "params": {...}}
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    server_type = body.get("server_type")
    tool = body.get("tool")
    params = body.get("params") or {}
    if not server_type or not tool:
        raise HTTPException(status_code=400, detail="server_type 与 tool 必填")
    return _call_wind(server_type, tool, params)
