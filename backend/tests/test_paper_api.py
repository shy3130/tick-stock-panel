"""虚拟账户 API 契约测试 — 成功/无数据/错误响应 (per CONTRIBUTING API 契约矩阵)。

镜像 test_alerts_query_bounds.py: 只挂本路由的裸 FastAPI app, 不经过主应用
全局中间件 (访问密码门是部署层关注点, 不属于本契约)。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "etf" if s.split(".")[0].startswith(("51", "56", "58", "15")) else "stock",
    )
    return TestClient(app)


def test_overview_before_init(client: TestClient):
    r = client.get("/api/paper/overview")
    assert r.status_code == 200
    assert r.json() == {"initialized": False, "account_id": "default"}


def test_multi_account_isolation_and_settings(client: TestClient):
    """双账户隔离: 订单/概览互不可见; settings 端点切换涨跌停排队。"""
    client.post("/api/paper/account", json={"initial_cash": 1000000})  # default
    r = client.post("/api/paper/account", json={"initial_cash": 500000, "account_id": "acc_a", "name": "策略A"})
    assert r.status_code == 200 and r.json()["account"]["name"] == "策略A"

    # 账户列表
    ids = {a["id"] for a in client.get("/api/paper/accounts").json()["accounts"]}
    assert ids == {"default", "acc_a"}

    # 订单隔离: 同一笔下单只出现在对应账户
    assert client.post("/api/paper/orders?account=acc_a", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 1500.0}).status_code == 200
    assert len(client.get("/api/paper/orders?account=acc_a").json()["orders"]) == 1
    assert len(client.get("/api/paper/orders").json()["orders"]) == 0

    # 概览隔离: acc_a 有 pending 单不影响 default 的现金展示
    assert client.get("/api/paper/overview?account=acc_a").json()["initial_cash"] == 500000
    assert client.get("/api/paper/overview").json()["initial_cash"] == 1000000

    # settings: 开启涨跌停排队 → 账户字段更新
    r = client.post("/api/paper/settings?account=acc_a", json={"queue_limit_orders": True})
    assert r.status_code == 200 and r.json()["account"]["queue_limit_orders"] is True
    assert client.get("/api/paper/overview?account=acc_a").json()["queue_limit_orders"] is True
    assert client.get("/api/paper/overview").json()["queue_limit_orders"] is False

    # 非法账户 id → 400 (路径穿越防护)
    assert client.get("/api/paper/overview?account=../x").status_code == 400


def test_account_create_idempotent(client: TestClient):
    r1 = client.post("/api/paper/account", json={"initial_cash": 500000})
    assert r1.status_code == 200
    r2 = client.post("/api/paper/account", json={"initial_cash": 900000})
    assert r2.status_code == 200
    assert r2.json()["account"]["cash"] == 500000  # 幂等: 不覆盖


def test_account_rejects_non_positive_cash(client: TestClient):
    assert client.post("/api/paper/account", json={"initial_cash": 0}).status_code == 400
    assert client.post("/api/paper/account", json={"initial_cash": -1}).status_code == 400


def test_order_create_list_cancel_flow(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 1500.0})
    assert r.status_code == 200
    order = r.json()["order"]
    assert order["status"] == "pending"

    # 非百股整数倍 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 150}).status_code == 400
    # 超资金 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100000, "ref_price": 1500.0}).status_code == 400
    # 卖出无持仓 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "sell", "qty": 100}).status_code == 400

    # 列表 + 撤单
    assert len(client.get("/api/paper/orders?status=pending").json()["orders"]) == 1
    r = client.delete(f"/api/paper/orders/{order['id']}")
    assert r.status_code == 200 and r.json()["order"]["status"] == "cancelled"
    # 已撤不可再撤
    assert client.delete(f"/api/paper/orders/{order['id']}").status_code == 400


def test_order_amount_mode_auto_converts(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "amount": 200000, "ref_price": 1500.0})
    assert r.status_code == 200
    assert r.json()["order"]["qty"] == 100  # 20万 / 1500 = 133 股 → 向下取整百 = 100
    # 无参考价的金额单 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "amount": 100}).status_code == 400


def test_freeze_blocks_new_orders(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 100000})
    assert client.post("/api/paper/freeze?frozen=true").status_code == 200
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 10.0})
    assert r.status_code == 400 and "冻结" in r.json()["detail"]
    # 解冻恢复
    assert client.post("/api/paper/freeze?frozen=false").status_code == 200
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 10.0}).status_code == 200


def test_freeze_before_init_rejected(client: TestClient):
    assert client.post("/api/paper/freeze?frozen=true").status_code == 400


def test_trades_nav_stats_empty(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 100000})
    assert client.get("/api/paper/trades").json() == {"fills": []}
    assert client.get("/api/paper/nav").json() == {"nav": []}
    stats = client.get("/api/paper/stats").json()
    assert stats["rounds"] == 0 and stats["win_rate"] == 0.0


def test_settings_updates_fees(client: TestClient):
    """费用三参数可经 settings 端点调整; 超范围 → 400; 未知字段忽略。"""
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/settings", json={
        "commission_pct": 0.0003, "stamp_tax_pct": 0.0005, "slippage_bps": 8,
    })
    assert r.status_code == 200
    acc = r.json()["account"]
    assert acc["commission_pct"] == 0.0003
    assert acc["stamp_tax_pct"] == 0.0005
    assert acc["slippage_bps"] == 8
    # 超范围
    r = client.post("/api/paper/settings", json={"commission_pct": 0.5})
    assert r.status_code == 400 and "超出合理范围" in r.json()["detail"]
    # 部分更新: 只改滑点, 佣金不动
    r = client.post("/api/paper/settings", json={"slippage_bps": 3})
    acc = r.json()["account"]
    assert acc["slippage_bps"] == 3 and acc["commission_pct"] == 0.0003


def test_compare_accounts(client: TestClient):
    """/compare: 逐账户概览+统计+净值; 未初始化的空壳账户不出现。"""
    # 只有懒创建的空壳目录时 → 空列表
    assert client.get("/api/paper/compare").json()["accounts"] == []

    client.post("/api/paper/account", json={"initial_cash": 1000000, "name": "甲"})
    client.post("/api/paper/account", json={"initial_cash": 500000, "account_id": "acc_b", "name": "乙"})
    rows = client.get("/api/paper/compare").json()["accounts"]
    assert [r["account"] for r in rows] == ["default", "acc_b"]
    row = rows[0]
    assert row["name"] == "甲" and row["initial_cash"] == 1000000
    assert row["total"] == pytest.approx(1000000)
    assert row["pnl_pct"] == 0.0
    assert row["fees"]["commission_pct"] == pytest.approx(0.00025)
    assert row["rounds"] == 0 and row["win_rate"] == 0.0
    assert row["nav"] == []  # 尚无定版净值
    # 字段完整性 (对比表依赖)
    for key in ("cash", "market_value", "total_pnl", "profit_loss_ratio", "max_drawdown", "avg_holding_days", "realized_pnl"):
        assert key in row
