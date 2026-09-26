"""tick-stock-panel MCP 服务器 — 开放接口的 AI 工具桥。

架构 (docs/features.md → 开放接口):
  AI 客户端 (Claude/ZCode/Cursor…) ↔ MCP stdio (JSON-RPC) ↔ 本服务器
  ↔ HTTP + Bearer Token ↔ 面板开放网关 (scope 校验 + 限流)

工具按 TSP_SCOPES 声明暴露 (逗号分隔, 默认 read:market,read:ext,read:analysis);
声明只是"展示哪些工具", 真正的权限裁决在面板网关 — 声明错了只会得到 403。
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("TSP_BASE", "http://127.0.0.1:3018").rstrip("/")
TOKEN = os.environ.get("TSP_TOKEN", "")
SCOPES = {s.strip() for s in os.environ.get(
    "TSP_SCOPES", "read:market,read:ext,read:analysis",
).split(",") if s.strip()}

mcp = MCPServer(
    "tick-stock-panel",
    instructions=(
        "A股行情、策略回测与监控面板。标的代码格式 600519.SH / 000001.SZ / 510300.SH;"
        "指数如 000001.SH(上证)。涉及交易/写数据的工具需面板侧授予相应权限。"
    ),
)

_client = httpx.Client(
    base_url=BASE,
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=60.0,
)


def _call(method: str, path: str, **kw) -> str:
    """调面板开放接口; 非 2xx 返回可读错误 (LLM 能据此纠正参数)。"""
    if not TOKEN:
        return "错误: 未设置 TSP_TOKEN (面板 → 设置 → 开放接口 → 新建 Token)"
    try:
        r = _client.request(method, path, **kw)
        r.raise_for_status()
        return r.text
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail", e.response.text[:200])
        except Exception:  # noqa: BLE001
            detail = e.response.text[:200]
        return f"HTTP {e.response.status_code}: {detail}"
    except httpx.HTTPError as e:
        return f"连接失败 ({BASE}): {e}"


def _has(scope: str) -> bool:
    return scope in SCOPES


# ── read:market 行情 ──────────────────────────────────

if _has("read:market"):

    @mcp.tool()
    def search_instrument(query: str, limit: int = 8) -> str:
        """按代码/名称/拼音首字母搜索标的, 返回 symbol+名称 (后续工具的 symbol 从这里来)。"""
        return _call("GET", "/api/kline/instruments/search", params={"q": query, "limit": limit})

    @mcp.tool()
    def get_kline(symbol: str, limit: int = 30) -> str:
        """标的日K (前复权): date/open/high/low/close/volume/amount, 最新在后。"""
        return _call("GET", "/api/kline/daily", params={"symbol": symbol, "limit": limit})

    @mcp.tool()
    def get_index_kline(symbol: str = "000001.SH", days: int = 60) -> str:
        """指数日K, 如 000001.SH 上证指数 / 399006.SZ 创业板指。"""
        return _call("GET", "/api/index/daily", params={"symbol": symbol, "days": days})

    @mcp.tool()
    def get_market_overview() -> str:
        """市场总览: 快照日期、涨跌家数、成交额、涨停/连板统计、情绪指标、行情源状态。"""
        return _call("GET", "/api/overview/market")


# ── read:analysis 策略/环境/告警 ───────────────────────

if _has("read:analysis"):

    @mcp.tool()
    def get_regime_state() -> str:
        """最新市场环境 (情绪周期阶段/环境分), 大盘择时判断的一手口径。"""
        return _call("GET", "/api/regime/latest")

    @mcp.tool()
    def list_strategies() -> str:
        """策略清单 (内置+自定义): id/名称/标签/描述 — run_backtest 的 strategy_id 从这里来。"""
        return _call("GET", "/api/strategies")

    @mcp.tool()
    def get_strategy_detail(strategy_id: str) -> str:
        """单个策略的完整定义 (信号条件/参数/评分)。"""
        return _call("GET", f"/api/strategies/{strategy_id}")

    @mcp.tool()
    def get_recent_alerts(limit: int = 20) -> str:
        """最近触发的监控告警 (信号/规则/异动, 时间倒序, 最多 7 天)。"""
        return _call("GET", "/api/alerts", params={"limit": limit})


# ── read:ext / write:ext 扩展数据 ─────────────────────

if _has("read:ext"):

    @mcp.tool()
    def list_ext_tables() -> str:
        """扩展表清单 (用户接入的自有数据表): id/名称/模式/字段。"""
        return _call("GET", "/api/ext-data")

    @mcp.tool()
    def query_ext_table(table: str, filter: str | None = None, limit: int = 20) -> str:
        """查询扩展表行数据。filter 形如 "字段:值1|值2" (可重复传多个条件参数)。"""
        params: dict = {"limit": limit}
        if filter:
            params["filter"] = filter
        return _call("GET", f"/api/ext-data/{table}/rows", params=params)

if _has("write:ext"):

    @mcp.tool()
    def write_ext_rows(table: str, rows: list[dict]) -> str:
        """向扩展表写入行数据 (会进入策略/回测数据面)。rows 每行须含表定义的必填字段。"""
        return _call("POST", f"/api/ext-data/{table}/ingest", json={"rows": rows})


# ── run:backtest 回测 ─────────────────────────────────

if _has("run:backtest"):

    @mcp.tool()
    def run_backtest(strategy_id: str, symbols: list[str], days: int = 90) -> str:
        """对指定标的跑策略回测 (纯 Polars 引擎), 返回指标/回合/净值。窗口别太大, 90~365 天为宜。"""
        from datetime import date, timedelta

        end = date.today()
        start = end - timedelta(days=days)
        return _call("POST", "/api/backtest/strategy/run", json={
            "strategy_id": strategy_id,
            "symbols": symbols,
            "start": start.isoformat(),
            "end": end.isoformat(),
        })


def main() -> None:
    if not TOKEN:
        raise SystemExit("未设置 TSP_TOKEN 环境变量 — 面板 → 设置 → 开放接口 → 新建 Token")
    mcp.run()  # stdio 传输, 由 MCP 客户端拉起


if __name__ == "__main__":
    main()
