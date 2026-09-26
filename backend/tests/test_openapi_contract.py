"""Tier A 开放契约快照 — 开放面的任何变化必须是有意识的。

契约即承诺: /api/openapi.json?tier=a 列出的端点集合是二开方依赖的稳定面。
本测试把该集合冻结成常量 — 新增/移除/改方法都会失败, 失败者须同步更新
快照并在 PR 里说明契约变更 (对外破坏性变更需 major 版本)。

快照来源与 /api/openapi.json?tier=a 完全同源: app.openapi() 的 paths
逐 (method, path) 过 api_gateway.required_scope — 规则表是唯一源。
"""
from __future__ import annotations

import pytest

from app.services import api_gateway

# 冻结的开放面 (method + OpenAPI 路径模板), 按字母序。
# 变更此表 = 契约变更, 请在 commit message 与 changelog 中明示。
EXPECTED_OPEN_ENDPOINTS: set[str] = {
    "GET /api/alerts",
    "GET /api/backtest/candidates",
    "POST /api/backtest/factor/batch",
    "GET /api/backtest/factor/columns",
    "POST /api/backtest/factor/run",
    "POST /api/backtest/run",
    "GET /api/backtest/status",
    "POST /api/backtest/strategy/run",
    "GET /api/events",
    "POST /api/events/ticket",
    "GET /api/ext-data",
    "POST /api/ext-data/{config_id}/ingest",
    "GET /api/ext-data/{config_id}/rows",
    "GET /api/ext-data/{config_id}/values",
    "GET /api/ext-data/schema-all",
    "GET /api/ext-data/schema/{config_id}",
    "GET /api/ext-data/{config_id}/dimension-intraday",
    "GET /api/ext-data/{config_id}/dimension-members",
    "GET /api/index/daily",
    "GET /api/index/minute",
    "GET /api/intraday/indices",
    "GET /api/intraday/status",
    "GET /api/kline/daily",
    "GET /api/kline/daily/latest",
    "GET /api/kline/instruments/search",
    "GET /api/kline/minute",
    "GET /api/kline/minute-range",
    "GET /api/overview/market",
    "GET /api/paper/account",
    "POST /api/paper/account",
    "GET /api/paper/accounts",
    "POST /api/paper/auto_rules",
    "DELETE /api/paper/auto_rules/{rule_id}",
    "GET /api/paper/auto_rules",
    "POST /api/paper/auto_rules/{rule_id}/enabled",
    "GET /api/paper/compare",
    "POST /api/paper/freeze",
    "GET /api/paper/nav",
    "POST /api/paper/orders",
    "DELETE /api/paper/orders/{order_id}",
    "GET /api/paper/orders",
    "GET /api/paper/overview",
    "GET /api/paper/positions",
    "POST /api/paper/rebuild",
    "POST /api/paper/settings",
    "GET /api/paper/stats",
    "GET /api/paper/trades",
    "GET /api/regime/coverage",
    "GET /api/regime/history",
    "GET /api/regime/latest",
    "GET /api/regime/mainline",
    "GET /api/regime/phases",
    "GET /api/regime/states",
    "GET /api/screener/cached",
    "GET /api/screener/limit-ladder",
    "GET /api/screener/market-snapshot",
    "POST /api/screener/run",
    "POST /api/screener/run_all",
    "POST /api/screener/run_preset",
    "GET /api/screener/strategies",
    "GET /api/strategies",
    "GET /api/strategies/ai/status",
    "GET /api/strategies/{strategy_id}",
    "GET /api/strategies/{strategy_id}/source",
}


def _collect_open_endpoints() -> set[str]:
    """与 /api/openapi.json?tier=a 同源收集 (app.openapi paths 过 required_scope)。"""
    from app.main import app

    spec = app.openapi()
    found: set[str] = set()
    for path, ops in spec.get("paths", {}).items():
        for method in ops:
            if method in ("get", "post", "put", "delete", "patch") and api_gateway.required_scope(
                method.upper(), path,
            ):
                found.add(f"{method.upper()} {path}")
    return found


def test_open_contract_snapshot():
    found = _collect_open_endpoints()
    added = found - EXPECTED_OPEN_ENDPOINTS
    removed = EXPECTED_OPEN_ENDPOINTS - found
    if added or removed:
        pytest.fail(
            "Tier A 开放契约发生变化 — 这是对外承诺面, 须有意识地更新快照:\n"
            f"  新增: {sorted(added) or '无'}\n"
            f"  移除: {sorted(removed) or '无'}\n"
            "确认无误后, 同步更新本文件的 EXPECTED_OPEN_ENDPOINTS 并在 commit 中说明。",
        )
