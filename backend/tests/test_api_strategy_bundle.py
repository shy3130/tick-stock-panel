"""策略包 API — 导出/inspect/apply 的 HTTP 层行为。

沿用 mini-app 模式: 只挂 strategies 路由, 手工注入 strategy_engine,
不进主应用 lifespan。服务层语义见 tests/test_strategy_bundle.py。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy as strategy_api
from app.factors import store as factor_store
from app.factors.registry import unregister_factor
from app.services import preferences
from app.strategy.engine import StrategyEngine

STANDALONE = '''\
META = {"id": "custom_standalone", "name": "独立", "description": "d", "tags": [],
       "asset_types": ["stock"], "timeframes": ["1d"],
       "scoring": {"uf_dep": 1.0}}

import polars as pl

SCORING = {"uf_dep": 1.0}


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''

UF_DEP = {"id": "uf_dep", "label": "依赖因子", "kind": "custom", "formula": "close", "lang": "dsl"}


def _write(path: Path, content: str | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(content, encoding="utf-8")


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences, "load", lambda: {})
    _write(tmp_path / "strategies" / "custom" / "custom_standalone.py", STANDALONE)
    _write(tmp_path / "user_data" / "custom_factors" / "uf_dep.json", UF_DEP)
    _write(
        tmp_path / "user_data" / "strategy_overrides" / "custom_standalone.json",
        '{"params": {"n": 5}}',
    )
    loaded = factor_store.load_into_registry(tmp_path)
    app = FastAPI()
    app.include_router(strategy_api.router)
    app.state.strategy_engine = StrategyEngine(strategy_dirs=[tmp_path / "strategies" / "custom"])
    yield TestClient(app)
    for fid in loaded:
        unregister_factor(fid)


def test_export_bundle(client: TestClient) -> None:
    r = client.get("/api/strategies/custom_standalone/bundle")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "tsp-strategy-custom_standalone.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert "strategies/custom/custom_standalone.py" in names
    assert "user_data/custom_factors/uf_dep.json" in names
    assert "user_data/strategy_overrides/custom_standalone.json" in names


def test_export_unknown_strategy_404(client: TestClient) -> None:
    assert client.get("/api/strategies/custom_nope/bundle").status_code == 404


def test_inspect_and_apply_roundtrip(
    client: TestClient, tmp_path_factory, monkeypatch
) -> None:
    from app.config import settings

    blob = client.get("/api/strategies/custom_standalone/bundle").content

    fresh = tmp_path_factory.mktemp("fresh")
    monkeypatch.setattr(settings, "data_dir", fresh)

    r = client.post(
        "/api/strategies/bundle/inspect",
        files={"file": ("tsp-strategy-custom_standalone.zip", blob, "application/zip")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == "custom_standalone"
    assert body["missing_deps"] == []
    assert {e["action"] for e in body["entries"]} == {"create"}

    r = client.post(
        "/api/strategies/bundle/apply",
        files={"file": ("tsp-strategy-custom_standalone.zip", blob, "application/zip")},
        data={"allow_overwrite": "false"},
    )
    assert r.status_code == 200
    assert (fresh / "strategies/custom/custom_standalone.py").is_file()
    by_key = {c["key"]: c for c in r.json()["categories"]}
    assert by_key["strategies"]["imported"] == 1
    assert by_key["custom_factors"]["imported"] == 1


def test_apply_rejects_out_of_scope(client: TestClient) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("user_data/watchlist.parquet", b"fake")
        zf.writestr(
            "manifest.json",
            json.dumps({"format": "tick-strategy-bundle", "version": 1, "strategy_id": "x"}),
        )
    r = client.post(
        "/api/strategies/bundle/apply",
        files={"file": ("evil.zip", buf.getvalue(), "application/zip")},
    )
    assert r.status_code == 400
    assert "不允许的条目" in r.json()["detail"]
