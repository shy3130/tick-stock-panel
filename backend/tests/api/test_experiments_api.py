"""F7 实验资产统一 API 契约 — 不读行情、不写真实 data/。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as bt_api
from app.backtest.optimizer import (
    OptimizerExperimentStore,
    SearchExperiment,
    SearchScenarioResult,
)
from app.backtest.parameter_grid import (
    GridExperiment,
    GridScenarioResult,
    ParameterGridExperimentStore,
)
from app.backtest.run_store import BacktestRun, BacktestRunStore


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(bt_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _save_optimizer(
    store: OptimizerExperimentStore,
    experiment_id: str,
    created_at: str,
    *,
    status: str = "completed",
    score: float | None = 1.5,
) -> None:
    scenario = SearchScenarioResult(
        scenario_id="sc-best",
        strategy_id="macd",
        strategy_label="MACD",
        universe_id="board:main",
        universe_label="主板",
        universe_kind="board",
        holding_days=5,
        matching="open_t+1",
        train_stats={"total_return": 0.2, "sharpe": 1.1},
        holdout_stats={"total_return": 0.08, "sharpe": 0.7},
        score=score,
        rank=1,
    )
    store.save(SearchExperiment(
        experiment_id=experiment_id,
        config_hash="h1",
        objective="sharpe",
        start="2024-01-01",
        end="2024-06-30",
        train_end="2024-04-30",
        holdout_start="2024-05-01",
        requested_count=1,
        scenario_count=1,
        max_scenarios=1,
        truncated=False,
        status=status,
        scenarios=[scenario],
        created_at=created_at,
    ))


def _save_grid(
    store: ParameterGridExperimentStore,
    experiment_id: str,
    created_at: str,
    *,
    status: str = "completed",
) -> None:
    scenario = GridScenarioResult(
        scenario_id="s0001",
        params={"fast": 12},
        stats={"total_return": 0.25, "sharpe": 1.4},
        score=1.75,
        rank=1,
    )
    store.save(GridExperiment(
        experiment_id=experiment_id,
        config_hash="g1",
        strategy_id="macd",
        objective="risk_adjusted",
        base_config={"strategy_id": "macd", "start": "2024-01-01", "end": "2024-03-31"},
        grid={"fast": [8, 12]},
        requested_count=2,
        scenario_count=2,
        max_scenarios=2,
        truncated=False,
        status=status,
        scenarios=[scenario],
        best_scenario_id="s0001",
        created_at=created_at,
    ))


def test_empty_list_returns_200(tmp_path):
    resp = _client(tmp_path).get("/api/backtest/experiments")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"items": [], "total": 0, "warnings": []}


def test_lists_persisted_experiments_from_both_stores(tmp_path):
    data_dir = tmp_path
    _save_optimizer(OptimizerExperimentStore(data_dir), "so-aaaaaaaaaaaa", "2026-08-01T10:00:00+00:00")
    _save_grid(ParameterGridExperimentStore(data_dir), "pg-bbbbbbbb", "2026-08-02T10:00:00+00:00")
    resp = _client(data_dir).get("/api/backtest/experiments")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    by_id = {row["id"]: row for row in body["items"]}
    assert by_id["so-aaaaaaaaaaaa"]["kind"] == "optimizer"
    assert by_id["pg-bbbbbbbb"]["kind"] == "grid"
    assert body["warnings"] == []


def test_schema_fields_complete(tmp_path):
    data_dir = tmp_path
    _save_optimizer(OptimizerExperimentStore(data_dir), "so-cccccccccccc", "2026-08-01T10:00:00+00:00")
    _save_grid(ParameterGridExperimentStore(data_dir), "pg-dddddddd", "2026-08-02T10:00:00+00:00")
    body = _client(data_dir).get("/api/backtest/experiments").json()
    assert len(body["items"]) == 2
    for row in body["items"]:
        assert set(row) == {
            "id", "kind", "title", "created_at", "status", "scenario_count",
            "best", "persisted", "run_count",
        }
        assert row["kind"] in {"optimizer", "grid"}
        assert row["persisted"] is True
        assert isinstance(row["scenario_count"], int)
        assert isinstance(row["run_count"], int)
        assert row["best"] is not None
        assert set(row["best"]) == {"label", "score", "total_return", "sharpe"}
        assert row["best"]["label"]
    by_id = {row["id"]: row for row in body["items"]}
    # 寻优最佳摘要优先留出期口径; 网格最佳摘来自最优场景 stats
    assert by_id["so-cccccccccccc"]["best"]["total_return"] == 0.08
    assert by_id["pg-dddddddd"]["best"]["sharpe"] == 1.4


def test_sorted_desc_by_created_at(tmp_path):
    data_dir = tmp_path
    opt_store = OptimizerExperimentStore(data_dir)
    _save_optimizer(opt_store, "so-111111111111", "2026-08-01T10:00:00+00:00")
    _save_optimizer(opt_store, "so-222222222222", "2026-08-03T10:00:00+00:00")
    grid_store = ParameterGridExperimentStore(data_dir)
    _save_grid(grid_store, "pg-33333333", "2026-08-02T10:00:00+00:00")
    body = _client(data_dir).get("/api/backtest/experiments").json()
    created = [row["created_at"] for row in body["items"]]
    assert created == sorted(created, reverse=True)
    assert [row["id"] for row in body["items"]] == [
        "so-222222222222", "pg-33333333", "so-111111111111",
    ]


def test_single_source_failure_degrades(tmp_path, monkeypatch):
    data_dir = tmp_path
    _save_grid(ParameterGridExperimentStore(data_dir), "pg-eeeeeeee", "2026-08-02T10:00:00+00:00")

    def _boom(_data_dir):
        raise RuntimeError("optimizer store unavailable")

    monkeypatch.setattr(bt_api, "_optimizer_experiment_rows", _boom)
    resp = _client(data_dir).get("/api/backtest/experiments")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["id"] for row in body["items"]] == ["pg-eeeeeeee"]
    assert body["total"] == 1
    assert any("寻优" in warning for warning in body["warnings"])


def test_run_count_uses_source_experiment_tag(tmp_path):
    data_dir = tmp_path
    _save_grid(ParameterGridExperimentStore(data_dir), "pg-ffffffff", "2026-08-02T10:00:00+00:00")
    run_store = BacktestRunStore(data_dir)
    run_store.save(BacktestRun(
        run_id="r1",
        kind="strategy",
        config={"strategy_id": "macd", "source_experiment_id": "pg-ffffffff"},
    ))
    run_store.save(BacktestRun(
        run_id="r2",
        kind="strategy",
        config={"strategy_id": "macd"},
    ))
    body = _client(data_dir).get("/api/backtest/experiments").json()
    by_id = {row["id"]: row for row in body["items"]}
    assert by_id["pg-ffffffff"]["run_count"] == 1
