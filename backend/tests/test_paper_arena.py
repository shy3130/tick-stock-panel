"""对比批量创建 + compare 对比增量字段 契约测试。

镜像 test_paper_api.py: 只挂 paper 路由的裸 FastAPI app; data_dir 用 tmp_path。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "stock",
    )
    return TestClient(app)


def test_arena_batch_create_accounts_and_rules(tmp_path: Path):
    """批量创建: 同本金/同费率 N 个账户, 各绑一条启用的自动跟单规则。"""
    c = _client(tmp_path)
    r = c.post("/api/paper/arena/batch_create", json={
        "initial_cash": 200000,
        "name_prefix": "对比-",
        "sources": [
            {"name": "均线多头", "match_kind": "strategy", "match_id": "sg_macd_golden"},
            {"match_kind": "rule", "match_id": "mrule_high_volume", "side": "sell", "size_value": 20},
        ],
    })
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert len(created) == 2
    assert created[0]["name"] == "对比-均线多头"
    # 未提供 name 的来源回退 match_id
    assert created[1]["name"] == "对比-mrule_high_volume"
    # 账户 id 服务端生成, 合法且互不相同
    ids = [row["account"] for row in created]
    assert all(i.startswith("arena_") for i in ids) and len(set(ids)) == 2

    # 账户同规格: 本金与费用一致
    for row in created:
        acc = c.get(f"/api/paper/account?account={row['account']}").json()["account"]
        assert acc["initial_cash"] == 200000 and acc["status"] == "active"
        assert acc["commission_pct"] > 0  # 默认费用

    # 各账户绑定的规则: 来源1 用默认 (买/10%权益/次日开盘), 来源2 覆盖 side/size
    r0 = c.get(f"/api/paper/auto_rules?account={ids[0]}").json()["rules"]
    assert len(r0) == 1 and r0[0]["enabled"] is True
    assert r0[0]["match_kind"] == "strategy" and r0[0]["match_id"] == "sg_macd_golden"
    assert r0[0]["side"] == "buy" and r0[0]["size_mode"] == "pct_equity" and r0[0]["size_value"] == 10
    r1 = c.get(f"/api/paper/auto_rules?account={ids[1]}").json()["rules"]
    assert r1[0]["side"] == "sell" and r1[0]["size_value"] == 20

    # compare 聚合出新账户与对比字段
    rows = {r["account"]: r for r in c.get("/api/paper/compare").json()["accounts"]}
    assert set(ids) <= set(rows)
    row = rows[ids[0]]
    assert row["holdings_count"] == 0 and row["last_nav_date"] is None and row["day_change_pct"] is None
    assert row["auto_enabled"] == 1
    assert row["auto_rules"][0]["match_id"] == "sg_macd_golden" and row["auto_rules"][0]["enabled"] is True


def test_arena_batch_create_validates_before_writing(tmp_path: Path):
    """任一来源非法 → 400 且不产生任何账户/规则 (fail-closed, 无半创建)。"""
    c = _client(tmp_path)
    for bad_sources in (
        [{"match_kind": "strategy", "match_id": ""}],                      # match_id 为空
        [{"match_kind": "nope", "match_id": "x"}],                         # match_kind 非法
        [{"match_kind": "strategy", "match_id": "x", "size_value": -1}],   # size_value 非正
    ):
        r = c.post("/api/paper/arena/batch_create", json={"initial_cash": 100000, "sources": bad_sources})
        assert r.status_code == 400, bad_sources
    # 非法请求不应留下任何账户 (compare 只看已初始化账户)
    assert c.get("/api/paper/compare").json()["accounts"] == []

    # 合法来源 + 非法本金同样整体拒绝
    r = c.post("/api/paper/arena/batch_create", json={
        "initial_cash": 0,
        "sources": [{"match_kind": "strategy", "match_id": "x"}],
    })
    assert r.status_code == 400
    assert c.get("/api/paper/compare").json()["accounts"] == []


def test_arena_batch_create_bounds(tmp_path: Path):
    """空来源 / 超过 20 个来源 → 400。"""
    c = _client(tmp_path)
    assert c.post("/api/paper/arena/batch_create", json={"initial_cash": 100000, "sources": []}).status_code == 400
    many = [{"match_kind": "strategy", "match_id": f"s{i}"} for i in range(21)]
    assert c.post("/api/paper/arena/batch_create", json={"initial_cash": 100000, "sources": many}).status_code == 400


def test_compare_arena_fields_with_nav(tmp_path: Path):
    """day_change_pct / last_nav_date 按定版净值序列末两点计算。"""
    c = _client(tmp_path)
    c.post("/api/paper/account", json={"initial_cash": 100000, "name": "甲"})
    nav_dir = tmp_path / "paper" / "accounts" / "default" / "nav"
    nav_dir.mkdir(parents=True, exist_ok=True)
    with (nav_dir / "daily.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"date": "2026-09-30", "nav": 100000.0}) + "\n")
        f.write(json.dumps({"date": "2026-10-04", "nav": 101500.0}) + "\n")
    row = c.get("/api/paper/compare").json()["accounts"][0]
    assert row["last_nav_date"] == "2026-10-04"
    assert row["day_change_pct"] == 1.5  # (101500/100000 - 1) * 100
