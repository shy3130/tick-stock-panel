"""叠加策略 API 端点测试 — save/composite/save + delete 409 防护。

使用 FastAPI TestClient + 临时 data_dir, 不依赖真实行情数据。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.strategy import router as strategy_router
from app.storage.repository import DataStore, KlineRepository
from app.strategy.engine import StrategyEngine


def _make_app(tmp_path: Path) -> TestClient:
    app = FastAPI()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = DataStore(data_dir)
    repo = KlineRepository(store)

    cdir = data_dir / "strategies" / "custom"
    composite_dir = data_dir / "strategies" / "composite"
    cdir.mkdir(parents=True, exist_ok=True)
    composite_dir.mkdir(parents=True, exist_ok=True)
    (cdir / "child_a.py").write_text(
        '''import polars as pl
META = {"id":"child_a","name":"A","asset_types":["stock"],"basic_filter":{"enabled":False},"scoring":{"close":1.0}}
def filter(df, params):
    return pl.col("close") > 0
''',
        encoding="utf-8",
    )

    engine = StrategyEngine(lambda _: pl.DataFrame(), strategy_dirs=[cdir, composite_dir])
    app.state.repo = repo
    app.state.strategy_engine = engine
    app.include_router(strategy_router)
    return TestClient(app)


def test_composite_save_creates_strategy(tmp_path):
    client = _make_app(tmp_path)
    resp = client.post(
        "/api/strategies/composite/save",
        json={
            "strategy_id": "composite_t1",
            "name": "T1",
            "children": [{"strategy_id": "child_a", "weight": 1.0}],
            "mode": "create",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["source"] == "composite"
    assert (tmp_path / "data" / "strategies" / "composite" / "composite_t1.py").exists()

    # 策略出现在列表
    resp = client.get("/api/strategies")
    ids = [s["id"] for s in resp.json()["strategies"]]
    assert "composite_t1" in ids


def test_composite_save_rejects_non_composite_id(tmp_path):
    client = _make_app(tmp_path)
    resp = client.post(
        "/api/strategies/composite/save",
        json={
            "strategy_id": "not_composite",
            "children": [{"strategy_id": "child_a", "weight": 1.0}],
            "mode": "create",
        },
    )
    assert resp.status_code == 400
    assert "composite_" in resp.json()["detail"]


def test_composite_save_rejects_missing_child(tmp_path):
    client = _make_app(tmp_path)
    resp = client.post(
        "/api/strategies/composite/save",
        json={
            "strategy_id": "composite_bad",
            "children": [{"strategy_id": "nonexistent", "weight": 1.0}],
            "mode": "create",
        },
    )
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]


def test_composite_save_rejects_nested_composite(tmp_path):
    client = _make_app(tmp_path)
    # 先创建一个 composite
    resp = client.post(
        "/api/strategies/composite/save",
        json={
            "strategy_id": "composite_inner",
            "children": [{"strategy_id": "child_a", "weight": 1.0}],
            "mode": "create",
        },
    )
    assert resp.status_code == 200
    # 再创建嵌套 composite
    resp = client.post(
        "/api/strategies/composite/save",
        json={
            "strategy_id": "composite_outer",
            "children": [{"strategy_id": "composite_inner", "weight": 1.0}],
            "mode": "create",
        },
    )
    assert resp.status_code == 400
    assert "嵌套" in resp.json()["detail"]


def test_delete_referenced_child_returns_409(tmp_path):
    client = _make_app(tmp_path)
    client.post(
        "/api/strategies/composite/save",
        json={
            "strategy_id": "composite_dep",
            "children": [{"strategy_id": "child_a", "weight": 1.0}],
            "mode": "create",
        },
    )
    resp = client.delete("/api/strategies/child_a")
    assert resp.status_code == 409
    assert "叠加策略" in resp.json()["detail"]


def test_strategy_detail_includes_composite_children(tmp_path):
    client = _make_app(tmp_path)
    client.post(
        "/api/strategies/composite/save",
        json={
            "strategy_id": "composite_detail",
            "children": [{"strategy_id": "child_a", "weight": 0.7}],
            "mode": "create",
        },
    )
    resp = client.get("/api/strategies/composite_detail")
    body = resp.json()
    assert body["execution_backend"] == "composite"
    assert body["composite_children"] is not None
    assert body["composite_children"][0]["id"] == "child_a"
    assert body["composite_children"][0]["weight"] == 0.7
    assert body["composite_children"][0]["name"] == "A"
