"""策略寻优 API 契约 — 不读行情、不写真实 data/。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import threading
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest_optimizer as opt_api
from app.backtest.optimizer import OptimizerExperimentStore, SearchExperiment, SearchScenarioResult


class _ImmediateThread:
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()

class _FrozenThread:
    """构造/start 均不执行 target：用于冻结工作线程，观察 claim 的落盘副作用。"""

    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        return None


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

def _interrupted_experiment(tmp_path: Path, *, with_request: bool = True) -> None:
    exp = SearchExperiment(
        experiment_id="so-0bbb00000001",
        config_hash="h1",
        objective="sharpe",
        start="2020-01-01",
        end="2026-08-14",
        train_end="2024-12-31",
        holdout_start="2025-01-01",
        requested_count=2,
        scenario_count=2,
        max_scenarios=2,
        truncated=False,
        status="interrupted",
        scenarios=[
            SearchScenarioResult(
                scenario_id="sc-keep0000001",
                strategy_id="macd",
                universe_id="all_a",
                universe_label="全A",
                universe_kind="all_a",
                holding_days=5,
                matching="open_t+1",
                train_stats={"sharpe": 1.0, "total_return": 0.2, "max_drawdown": -0.1, "n_trades": 30, "pending_exit_positions": 0},
                score=1.0,
            )
        ],
        created_at="2026-01-01T00:00:00+00:00",
        request={"strategy_ids": ["macd"], "include_all_a": True, "boards": ["main"], "holding_days": [5], "years": 8} if with_request else None,
    )
    OptimizerExperimentStore(tmp_path).save(exp)


def test_launch_passes_request_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(opt_api, "_get_engine", lambda _req: object())
    monkeypatch.setattr(opt_api.threading, "Thread", _ImmediateThread)
    captured: dict = {}

    def _fake_run(service, store, **kwargs):
        captured.update(kwargs)
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
        )
        store.save(exp)
        return exp

    monkeypatch.setattr(opt_api, "run_search", _fake_run)
    client = _client(tmp_path)
    resp = client.post("/api/backtest/optimizer", json={
        "strategy_ids": ["macd"],
        "include_all_a": True,
        "boards": ["main"],
        "holding_days": [5],
        "years": 8,
    })
    assert resp.status_code == 200, resp.text
    snapshot = captured["request_snapshot"]
    assert snapshot["strategy_ids"] == ["macd"]
    assert snapshot["boards"] == ["main"]
    assert captured["existing"] is None


def test_resume_missing_experiment_is_404(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/backtest/optimizer/so-0ccc00000001/resume")
    assert resp.status_code == 404


def test_resume_without_request_snapshot_is_409(tmp_path):
    _interrupted_experiment(tmp_path, with_request=False)
    client = _client(tmp_path)
    resp = client.post("/api/backtest/optimizer/so-0bbb00000001/resume")
    assert resp.status_code == 409
    assert "缺少原始请求快照" in resp.json()["detail"]


def test_resume_cancelled_is_409(tmp_path):
    _interrupted_experiment(tmp_path)
    store = OptimizerExperimentStore(tmp_path)
    exp = store.load("so-0bbb00000001")
    exp.status = "cancelled"
    store.save(exp)
    client = _client(tmp_path)
    resp = client.post("/api/backtest/optimizer/so-0bbb00000001/resume")
    assert resp.status_code == 409
    assert "cancelled" in resp.json()["detail"]


def test_resume_already_running_returns_conflict_free_payload(tmp_path):
    _interrupted_experiment(tmp_path)
    client = _client(tmp_path)
    job = opt_api._OptJob("so-0bbb00000001", "h1")
    opt_api._jobs["so-0bbb00000001"] = job
    try:
        resp = client.post("/api/backtest/optimizer/so-0bbb00000001/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_running"
    finally:
        opt_api._jobs.pop("so-0bbb00000001", None)


def test_resume_uses_frozen_window_and_existing(tmp_path, monkeypatch):
    _interrupted_experiment(tmp_path)

    def _no_resolve(*_a, **_k):
        raise AssertionError("resume 禁止重新 resolve_window")

    monkeypatch.setattr(opt_api, "resolve_window", _no_resolve)
    monkeypatch.setattr(opt_api, "_get_engine", lambda _req: object())
    monkeypatch.setattr(opt_api.threading, "Thread", _ImmediateThread)
    captured: dict = {}

    def _fake_run(service, store, **kwargs):
        captured.update(kwargs)
        exp = kwargs["existing"]
        exp.status = "completed"
        store.save(exp)
        return exp

    monkeypatch.setattr(opt_api, "run_search", _fake_run)
    client = _client(tmp_path)
    resp = client.post("/api/backtest/optimizer/so-0bbb00000001/resume")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resumed"
    assert body["start"] == "2020-01-01"
    assert body["train_end"] == "2024-12-31"
    assert body["holdout_start"] == "2025-01-01"
    assert body["end"] == "2026-08-14"
    # 窗口取磁盘冻结值, 而不是按当前数据重解
    assert captured["train_start"] == date(2020, 1, 1)
    assert captured["train_end"] == date(2024, 12, 31)
    assert captured["holdout_start"] == date(2025, 1, 1)
    assert captured["holdout_end"] == date(2026, 8, 14)
    # 续跑复用磁盘实验（保留已完成场景）, 且场景数一致
    assert captured["existing"].experiment_id == "so-0bbb00000001"
    assert captured["existing"].scenarios[0].scenario_id == "sc-keep0000001"
    assert body["scenario_count"] == 2
    detail = client.get("/api/backtest/optimizer/so-0bbb00000001")
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"


def test_stream_replays_done_snapshot(tmp_path):
    _interrupted_experiment(tmp_path)
    job = opt_api._OptJob("so-0bbb00000001", "h1")
    job.done = True
    job.finish_ts = 0.0
    job.snapshot = {"experiment_id": "so-0bbb00000001", "status": "completed"}
    opt_api._jobs["so-0bbb00000001"] = job
    try:
        client = _client(tmp_path)
        with client.stream("GET", "/api/backtest/optimizer/so-0bbb00000001/stream") as resp:
            assert resp.status_code == 200
            body = "".join(chunk.decode("utf-8") for chunk in resp.iter_raw())
        assert "event: done" in body
        assert "completed" in body
    finally:
        opt_api._jobs.pop("so-0bbb00000001", None)


def test_resume_marks_disk_running_before_return(tmp_path, monkeypatch):
    """resume 返回前磁盘必须已是 running，GET 不得读到旧的 interrupted。"""
    _interrupted_experiment(tmp_path)
    monkeypatch.setattr(opt_api, "_get_engine", lambda _req: object())
    monkeypatch.setattr(opt_api.threading, "Thread", _FrozenThread)
    client = _client(tmp_path)
    try:
        resp = client.post("/api/backtest/optimizer/so-0bbb00000001/resume")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "resumed"
        assert OptimizerExperimentStore(tmp_path).load("so-0bbb00000001").status == "running"
        detail = client.get("/api/backtest/optimizer/so-0bbb00000001")
        assert detail.status_code == 200
        assert detail.json()["status"] == "running"
    finally:
        opt_api._jobs.pop("so-0bbb00000001", None)


def test_concurrent_resume_starts_exactly_one_thread(tmp_path, monkeypatch):
    """两线程同时 resume：恰好一次 Thread 构造/start，另一个 already_running。"""
    _interrupted_experiment(tmp_path)
    monkeypatch.setattr(opt_api, "_get_engine", lambda _req: object())
    calls = {"built": 0, "started": 0}

    class _CountingFrozenThread:
        def __init__(self, target, daemon=None):
            calls["built"] += 1

        def start(self):
            calls["started"] += 1

    # 只替换 opt_api 命名空间内的 threading，避免污染全局 threading 模块
    monkeypatch.setattr(opt_api, "threading", SimpleNamespace(Thread=_CountingFrozenThread, Event=threading.Event))
    client_a = _client(tmp_path)
    client_b = _client(tmp_path)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def _resume(client):
        barrier.wait(timeout=10)
        outcomes.append(client.post("/api/backtest/optimizer/so-0bbb00000001/resume"))

    workers = [threading.Thread(target=_resume, args=(c,)) for c in (client_a, client_b)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    try:
        assert len(outcomes) == 2
        assert all(r.status_code == 200 for r in outcomes), [r.text for r in outcomes]
        assert sorted(r.json()["status"] for r in outcomes) == ["already_running", "resumed"]
        assert calls["built"] == 1
        assert calls["started"] == 1
        assert OptimizerExperimentStore(tmp_path).load("so-0bbb00000001").status == "running"
    finally:
        opt_api._jobs.pop("so-0bbb00000001", None)


def test_resume_worker_failure_persists_failed(tmp_path, monkeypatch):
    """run_search 抛异常时实验落盘 failed 且 error 带原因。"""
    _interrupted_experiment(tmp_path)
    monkeypatch.setattr(opt_api, "_get_engine", lambda _req: object())
    monkeypatch.setattr(opt_api.threading, "Thread", _ImmediateThread)

    def _boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(opt_api, "run_search", _boom)
    client = _client(tmp_path)
    try:
        resp = client.post("/api/backtest/optimizer/so-0bbb00000001/resume")
        assert resp.status_code == 200, resp.text
        exp = OptimizerExperimentStore(tmp_path).load("so-0bbb00000001")
        assert exp.status == "failed"
        assert "boom" in (exp.error or "")
        detail = client.get("/api/backtest/optimizer/so-0bbb00000001")
        assert detail.status_code == 200
        assert detail.json()["status"] == "failed"
    finally:
        opt_api._jobs.pop("so-0bbb00000001", None)
