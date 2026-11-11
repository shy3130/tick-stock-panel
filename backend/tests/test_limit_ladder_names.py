from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.screener import router


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("cache_source", ["latest", "history"])
def test_ladder_fills_partial_names_from_instruments(tmp_path, direction, cache_source):
    as_of = date(2026, 11, 10)
    symbols = ["002623.SZ", "002708.SZ", "603706.SH", "600000.SH", "000001.SZ"]
    snapshot = pl.DataFrame({
        "symbol": symbols, "date": [as_of] * 5,
        "name": [None, "", "   ", "已有名称", None],
        "close": [11.0] * 5, "open": [11.0] * 5, "high": [11.0] * 5, "low": [11.0] * 5,
        "signal_limit_up": [direction == "up"] * 5,
        "signal_limit_down": [direction == "down"] * 5,
        "consecutive_limit_ups": [3] * 5, "consecutive_limit_downs": [3] * 5,
    })
    original = snapshot.clone()
    instruments = pl.DataFrame({
        "symbol": symbols[:4], "name": ["测试甲", "测试乙", "测试丙", "其他名称"],
    })
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest_asset=lambda asset: (snapshot, as_of if cache_source == "latest" else as_of + timedelta(days=1)),
        get_enriched_history=lambda *args: snapshot,
        get_instruments_asset=lambda asset: instruments,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    app.state.depth_service = SimpleNamespace(
        get_sealed_map=lambda *args, **kwargs: {}, is_sealed_ready=lambda *args: False,
    )
    with TestClient(app) as client:
        response = client.get("/api/screener/limit-ladder", params={"as_of": str(as_of), "direction": direction})
        # 同一份行情缓存随后仍应读取更新后的标的信息。
        instruments = instruments.with_columns(pl.lit("更新名称").alias("name"))
        updated = client.get("/api/screener/limit-ladder", params={"as_of": str(as_of), "direction": direction})
    assert response.status_code == 200, response.text
    tier, = response.json()["tiers"]
    assert tier["boards"] == 3 and tier["count"] == 5
    by_symbol = {row["symbol"]: row for row in tier["stocks"]}
    assert [by_symbol[s]["name"] for s in symbols] == ["测试甲", "测试乙", "测试丙", "已有名称", None]
    assert all(row["is_one_word"] for row in tier["stocks"])
    updated_names = {row["symbol"]: row["name"] for row in updated.json()["tiers"][0]["stocks"]}
    assert updated_names[symbols[0]] == "更新名称"
    assert updated_names[symbols[3]] == "已有名称"
    assert snapshot.equals(original)
