"""模拟盘账户删除 / 回合明细 / 对比盘中估算 契约测试。

删除是破坏性操作 (per CONTRIBUTING §8): 必须覆盖 fail-closed 拒绝、force 强删、
不存在账户、路径穿越防护; 强删后数据目录必须整体消失。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router
from app.market_time import cn_today


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "stock",
    )
    return TestClient(app)


def _mk_account(client: TestClient, acc: str = "del_me", cash: float = 1000000) -> None:
    r = client.post("/api/paper/account", json={"initial_cash": cash, "account_id": acc})
    assert r.status_code == 200


def test_delete_empty_account(client: TestClient, tmp_path: Path):
    _mk_account(client)
    r = client.delete("/api/paper/account?account=del_me")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not (tmp_path / "paper" / "accounts" / "del_me").exists()
    ids = {a["id"] for a in client.get("/api/paper/accounts").json()["accounts"]}
    assert "del_me" not in ids


def test_delete_missing_account_404(client: TestClient):
    r = client.delete("/api/paper/account?account=nope")
    assert r.status_code == 404


def test_delete_rejects_invalid_id(client: TestClient):
    assert client.delete("/api/paper/account?account=../escape").status_code == 400


def test_delete_with_pending_order_requires_force(client: TestClient, tmp_path: Path):
    _mk_account(client)
    assert client.post(
        "/api/paper/orders?account=del_me",
        json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 1500.0},
    ).status_code == 200
    r = client.delete("/api/paper/account?account=del_me")
    assert r.status_code == 400 and "force" in r.json()["detail"]
    assert (tmp_path / "paper" / "accounts" / "del_me" / "account.json").exists()  # 拒绝时不动数据

    r = client.delete("/api/paper/account?account=del_me&force=true")
    assert r.status_code == 200
    assert not (tmp_path / "paper" / "accounts" / "del_me").exists()


def test_delete_with_holding_requires_force(client: TestClient, tmp_path: Path):
    """持仓存在时同样 fail-closed; 直接写台账构造持仓 (绕过撮合, 聚焦删除语义)。"""
    _mk_account(client)
    fills = tmp_path / "paper" / "accounts" / "del_me" / "fills.jsonl"
    fills.parent.mkdir(parents=True, exist_ok=True)
    fill = {
        "seq": 1, "ts": "2026-10-14T10:00:00+08:00", "date": "2026-10-13",
        "order_id": "o1", "symbol": "600519.SH", "asset_type": "stock",
        "side": "buy", "qty": 100, "price": 1500.0, "fee": 5.0, "kind": "fill",
    }
    fills.write_text(json.dumps(fill, ensure_ascii=False) + "\n", encoding="utf-8")
    # 物化持仓 (rebuild 由台账重建)
    assert client.post("/api/paper/rebuild?account=del_me").status_code == 200

    r = client.delete("/api/paper/account?account=del_me")
    assert r.status_code == 400 and "持仓" in r.json()["detail"]
    assert client.delete("/api/paper/account?account=del_me&force=true").status_code == 200


def test_rounds_endpoint_empty_and_filled(client: TestClient, tmp_path: Path):
    _mk_account(client)
    assert client.get("/api/paper/rounds?account=del_me").json() == {"rounds": []}

    # 一买一卖 → 一个完整回合
    fills = tmp_path / "paper" / "accounts" / "del_me" / "fills.jsonl"
    fills.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"seq": 1, "ts": "2026-10-10T10:00:00+08:00", "date": "2026-10-10", "order_id": "o1",
         "symbol": "600519.SH", "asset_type": "stock", "side": "buy", "qty": 100,
         "price": 100.0, "fee": 5.0, "kind": "fill"},
        {"seq": 2, "ts": "2026-10-13T10:00:00+08:00", "date": "2026-10-13", "order_id": "o2",
         "symbol": "600519.SH", "asset_type": "stock", "side": "sell", "qty": 100,
         "price": 110.0, "fee": 6.0, "kind": "fill"},
    ]
    fills.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    rounds = client.get("/api/paper/rounds?account=del_me").json()["rounds"]
    assert len(rounds) == 1
    rt = rounds[0]
    assert rt["symbol"] == "600519.SH"
    assert rt["open_date"] == "2026-10-10" and rt["close_date"] == "2026-10-13"
    assert rt["qty"] == 100
    # 盈亏 = 卖出净额 (110*100-6) - 买入成本 (100*100+5) = 10994 - 10005 = 989
    assert rt["pnl"] == 989.0
    assert rt["holding_days"] == 3


def test_compare_without_quote_service_uses_settled(client: TestClient):
    """无 quote_service (或快照失败) 时 live 字段为空, 定版口径不受影响。"""
    _mk_account(client, "cmp_a")
    rows = client.get("/api/paper/compare").json()["accounts"]
    assert len(rows) == 1
    assert rows[0]["live_total"] is None and rows[0]["estimating"] is False


def test_compare_with_live_snapshot(client: TestClient, tmp_path: Path):
    """当日快照就绪 → live_total 按快照价估算 (盘中口径), 定版字段并存。"""
    _mk_account(client, "cmp_a")
    fills = tmp_path / "paper" / "accounts" / "cmp_a" / "fills.jsonl"
    fills.parent.mkdir(parents=True, exist_ok=True)
    fills.write_text(json.dumps({
        "seq": 1, "ts": "2026-10-13T10:00:00+08:00", "date": "2026-10-13", "order_id": "o1",
        "symbol": "600519.SH", "asset_type": "stock", "side": "buy", "qty": 100,
        "price": 100.0, "fee": 5.0, "kind": "fill",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    client.post("/api/paper/rebuild?account=cmp_a")

    # mock quote_service: 当日 enriched 快照, 现价 120 → 估算总资产 = 现金 + 100*120
    enriched = pl.DataFrame({"symbol": ["600519.SH"], "raw_close": [120.0]})
    client.app.state.quote_service = SimpleNamespace(get_enriched_today=lambda: (enriched, cn_today()))

    rows = client.get("/api/paper/compare").json()["accounts"]
    assert len(rows) == 1
    row = rows[0]
    assert row["estimating"] is True
    # 现金 1000000 - 10005 = 989995; 市值 100*120 = 12000 → live_total 1001995
    assert row["live_total"] == pytest.approx(1001995.0, abs=0.01)
    assert row["live_pnl_pct"] == pytest.approx(0.1995, abs=0.001)
