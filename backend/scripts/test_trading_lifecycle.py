#!/usr/bin/env python3
"""Trading 域端到端冒烟。

使用临时 data_dir + FastAPI TestClient,不会修改 data/ 用户数据。
覆盖: 建档→准备→成交→组合快照(fhold 段)→审计→红旗→计划偏差→提案→策略体检。

运行:
    cd backend
    uv run python scripts/test_trading_lifecycle.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings


def main() -> int:
    settings.data_dir = Path(tempfile.mkdtemp(prefix="tickflow-trading-smoke-"))
    # main 必须在 data_dir 重定向之后导入,避免 startup 组件捕获真实目录。
    from app.main import app

    client = TestClient(app, headers={"x-forwarded-for": "127.0.0.1"})

    def must(response, code: int = 200):
        if response.status_code != code:
            raise AssertionError(f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text}")
        return response.json()

    must(client.put("/api/trading/accounts", json={
        "accounts": [{
            "id": "default", "currency": "CNY", "capital": 500_000,
            "horizonFundMonths": 12, "maxSingleRatio": 0.25, "changes": [],
        }],
    }))

    trade = must(client.post("/api/trading/trades", json={
        "symbol": "600519.SH", "name": "贵州茅台", "strategy": "smoke",
        "thesis": {"text": "突破年线", "invalidation": "跌回年线下方三日不能收回"},
        "stopLoss": 1600, "plannedQty": 10, "plannedPrice": 1680,
    }))
    trade_id = trade["tradeId"]

    must(client.post(f"/api/trading/trades/{trade_id}/events", json={
        "kind": "prepare", "payload": {"plannedQty": 10, "plannedPrice": 1680, "stopLoss": 1600},
    }))
    trade = must(client.post(f"/api/trading/trades/{trade_id}/events", json={
        "kind": "fill", "payload": {"qty": 10, "price": 1680},
    }))
    assert trade["status"] == "持仓中" and trade["position"]["invested"] == 16_800

    snapshot = must(client.get("/api/trading/portfolio"))
    assert "positions" in snapshot and "fhold" in snapshot

    detail = must(client.get(f"/api/trading/trades/{trade_id}"))
    assert [event["kind"] for event in detail["events"]] == ["open", "prepare", "fill"]

    audit = must(client.get("/api/trading/audit", params={"trade_id": trade_id}))
    assert len(audit["audit"]) >= 3

    flags = must(client.get(f"/api/trading/trades/{trade_id}/red-flags"))
    assert "flags" in flags

    date = "20260804"
    must(client.put(f"/api/trading/plans/{date}", json={
        "replace": True,
        "entries": [{
            "id": "smoke-plan", "symbol": "600519.SH", "action": "buy_new",
            "trigger": "突破", "qty": 10, "reason": "冒烟",
        }],
    }))
    deviation = must(client.get(f"/api/trading/plans/{date}/deviation"))
    assert {"planned_but_not_done", "done_but_not_planned", "matched"} <= deviation.keys()

    proposal = must(client.post("/api/trading/proposals", json={
        "title": "冒烟提案", "target": "smoke", "evidence": ["event"],
        "falsifier": "下一批样本未改善", "sampleSize": 10,
    }))
    assert proposal["status"] == "draft"

    must(client.put("/api/strategies/smoke/profile", json={
        "invalidation": [{"name": "趋势失效", "observable": "跌破年线", "action": "退出"}],
        "risk": {"positionLimitPct": 20, "lossBudgetPct": 5, "thesisHorizonMonths": 6},
        "cadence": {"review": "weekly"},
    }))
    diagnosis = must(client.get("/api/strategies/smoke/profile/validate"))
    assert diagnosis["checks"]

    print("Trading lifecycle smoke: PASS")
    print(f"  trade: {trade_id}")
    print(f"  events: {len(detail['events'])}; audit: {len(audit['audit'])}; flags: {len(flags['flags'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
