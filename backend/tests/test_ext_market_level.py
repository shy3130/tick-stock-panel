"""市场级扩展表 (market_level, 无 symbol 列) 全链路 + /values 枚举端点。

市场级表面向"行 = 全市场每日一条"的数据 (择时状态/情绪指数), 是把
市场环境类数据发布为扩展数据、供 /rows API 与未来"市场级过滤数据集"
契约消费的前提。标的表的既有强制 (逐行 symbol) 行为不变。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ext_data import router
from app.services.ext_data import ExtConfig, ExtField, PullConfig


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # db=None: _refresh_views 的 db.execute 全在 try/except 内, 裸 app 下安全跳过
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path, db=None))
    return TestClient(app)


def _create(client: TestClient, market_level: bool = False, cid: str = "regime_ts") -> None:
    r = client.post("/api/ext-data", json={
        "id": cid,
        "label": "市场状态",
        "mode": "timeseries",
        "fields": [{"name": "state", "dtype": "string"}, {"name": "score", "dtype": "float"}],
        "market_level": market_level,
    })
    assert r.status_code == 200, r.text


def test_market_level_ingest_and_rows_roundtrip(client: TestClient):
    _create(client, market_level=True)
    r = client.post("/api/ext-data/regime_ts/ingest", json={
        "date": "2026-09-24",
        "rows": [{"state": "bull", "score": 80.0}],
    })
    assert r.status_code == 200, r.text
    rows = client.get("/api/ext-data/regime_ts/rows").json()
    assert rows["total"] == 1 and rows["rows"][0]["state"] == "bull"


def test_symbol_table_still_requires_symbol(client: TestClient):
    _create(client, market_level=False, cid="per_stock")
    r = client.post("/api/ext-data/per_stock/ingest", json={
        "date": "2026-09-24",
        "rows": [{"state": "bull", "score": 80.0}],   # 无 symbol
    })
    assert r.status_code == 400 and "缺少 symbol" in r.json()["detail"]


def test_market_level_flag_persists(client: TestClient):
    _create(client, market_level=True)
    cfg = client.get("/api/ext-data").json()["items"][0]
    assert cfg["market_level"] is True
    # update 可切换
    r = client.put("/api/ext-data/regime_ts", json={"market_level": False})
    assert r.json()["market_level"] is False


async def test_market_level_pull_skips_symbol_check(monkeypatch: pytest.MonkeyPatch):
    """拉取链路: 市场级表响应行无 symbol/code 也不报错 (标的表会 400)。"""
    from app.services import ext_pull

    async def fake(pull, config_id, day=None, extra_params=None):
        return [{"state": "bull", "score": 80.0}]   # 直接根数组

    monkeypatch.setattr(ext_pull, "_request_json", fake)
    cfg = ExtConfig(
        id="regime_ts", label="市场状态", mode="timeseries",
        fields=[ExtField("state", "string"), ExtField("score", "float")],
        pull=PullConfig(url="https://api.example.com/regime"),
        market_level=True,
    )
    rows = await ext_pull.fetch_rows_for_date(cfg, date(2026, 9, 24))
    assert rows == [{"state": "bull", "score": 80.0}]

    # 对照: 标的表同样响应 → 报错
    cfg2 = ExtConfig(
        id="per_stock", label="个股", mode="timeseries",
        fields=[ExtField("state", "string")],
        pull=PullConfig(url="https://api.example.com/x"),
    )
    with pytest.raises(ValueError, match="symbol"):
        await ext_pull.fetch_rows_for_date(cfg2, date(2026, 9, 24))


def test_values_endpoint_counts_and_400(client: TestClient):
    r = client.post("/api/ext-data", json={
        "id": "tags", "label": "标签", "mode": "timeseries",
        "fields": [{"name": "state", "dtype": "string"}],
    })
    assert r.status_code == 200
    client.post("/api/ext-data/tags/ingest", json={
        "date": "2026-09-24",
        "rows": [
            {"symbol": "000001.SZ", "state": "bull"},
            {"symbol": "600519.SH", "state": "bull"},
            {"symbol": "300750.SZ", "state": "bear"},
        ],
    })
    r = client.get("/api/ext-data/tags/values", params={"field": "state"})
    assert r.status_code == 200
    body = r.json()
    assert body["distinct"] == 2
    assert body["values"] == [{"value": "bull", "count": 2}, {"value": "bear", "count": 1}]
    # 未知字段 → 400
    assert client.get("/api/ext-data/tags/values", params={"field": "nope"}).status_code == 400
