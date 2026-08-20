"""策略寻优 API 契约 — 不读行情、不写真实 data/。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest_optimizer as opt_api
from app.backtest.optimizer import SearchExperiment


class _ImmediateThread:
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class _FakeStrategyEngine:
    def get(self, strategy_id: str):
        if strategy_id == "missing":
            raise ValueError("unknown strategy: missing")
        # 为寻优参数网格测试提供最小 params 定义
        params = []
        if strategy_id in ("macd", "boll_breakout"):
            params = [{"id": "vol_ratio_min", "type": "float", "default": 1.5}]
        return SimpleNamespace(
            id=strategy_id,
            execution_backend="polars_expr",
            source="builtin",
            ephemeral=False,
            meta={"asset_types": ["stock"], "params": params},
        )

    def put_ephemeral(self, strategy_id: str, strategy) -> None:
        return None

def _client(tmp_path: Path, *, latest=date(2026, 8, 14), earliest=date(2016, 1, 1)) -> TestClient:
    app = FastAPI()
    app.include_router(opt_api.router)
    frame = pl.DataFrame({
        "symbol": ["600000.SH", "300750.SZ", "688981.SH", "920001.BJ"],
    })
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        local_enriched_latest_date=lambda: latest,
        earliest_daily_date=lambda: earliest,
        get_enriched_latest=lambda: (frame, latest),
    )
    app.state.strategy_engine = _FakeStrategyEngine()
    app.state.backtest_engine = object()
    return TestClient(app)


def test_universes_lists_boards_and_window(tmp_path, monkeypatch):
    monkeypatch.setattr(opt_api, "symbol_dimension_map", lambda *_a, **_k: {
        "600000.SH": ["银行"],
        "601398.SH": ["银行"],
        "300750.SZ": ["电力设备"],
    })
    client = _client(tmp_path)
    resp = client.get("/api/backtest/optimizer/universes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["years"] == 8
    assert body["end"] == "2026-08-14"
    assert body["start"] == "2018-08-14"
    assert {row["id"] for row in body["boards"]} == {"main", "gem", "star", "bj"}
    assert body["industries"][0]["id"] == "银行"
    assert "survivorship_bias" in body["warnings"]


def test_launch_rejects_unknown_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(opt_api, "_get_engine", lambda _req: object())
    client = _client(tmp_path)
    resp = client.post("/api/backtest/optimizer", json={
        "strategy_ids": ["missing"],
        "include_all_a": True,
        "boards": [],
    })
    assert resp.status_code == 400
    assert "unknown strategy" in resp.json()["detail"]


def test_launch_rejects_empty_universe(tmp_path, monkeypatch):
    monkeypatch.setattr(opt_api, "_get_engine", lambda _req: object())
    client = _client(tmp_path)
    resp = client.post("/api/backtest/optimizer", json={
        "strategy_ids": ["macd"],
        "include_all_a": False,
        "boards": [],
        "industries": [],
        "industry_top_n": 0,
    })
    assert resp.status_code == 422
    assert "股票池" in resp.json()["detail"]


def test_launch_runs_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(opt_api, "_get_engine", lambda _req: object())
    monkeypatch.setattr(opt_api.threading, "Thread", _ImmediateThread)

    def _fake_run(service, store, **kwargs):
        exp = SearchExperiment(
            experiment_id=kwargs["experiment_id"],
            config_hash=kwargs["config_hash"],
            objective=kwargs["objective"],
            start=kwargs["train_start"].isoformat(),
            end=kwargs["holdout_end"].isoformat(),
            train_end=kwargs["train_end"].isoformat(),
            holdout_start=kwargs["holdout_start"].isoformat(),
            requested_count=kwargs["requested_count"],
            scenario_count=kwargs["requested_count"],
            max_scenarios=kwargs["requested_count"],
            truncated=kwargs["truncated"],
            status="completed",
            recommended_ids=[],
            diagnostics={"dsr": 0.4, "pbo": {"pbo": 0.5}},
            warnings=["survivorship_bias"],
        )
        store.save(exp)
        return exp

    monkeypatch.setattr(opt_api, "run_search", _fake_run)
    client = _client(tmp_path)
    resp = client.post("/api/backtest/optimizer", json={
        "strategy_ids": ["macd", "boll_breakout"],
        "include_all_a": True,
        "boards": ["main"],
        "holding_days": [5, 10],
        "years": 8,
    })
    assert resp.status_code == 200, resp.text
    launched = resp.json()
    assert launched["status"] == "started"
    assert launched["scenario_count"] == 12
    assert launched["truncated"] is False
    detail = client.get(f"/api/backtest/optimizer/{launched['experiment_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "completed"
    assert body["diagnostics"]["dsr"] == 0.4
    assert "survivorship_bias" in body["warnings"]


def test_missing_experiment_is_404(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/backtest/optimizer/so-deadbeef0001")
    assert resp.status_code == 404
