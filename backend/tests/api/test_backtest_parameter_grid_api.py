"""参数网格 API 契约 — resume 语义, 不读行情、不写真实 data/。"""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

# monkeypatch 会替换全局 threading.Thread (pg_api.threading 即该模块),
# 导入期留存真 Thread 供测试自身起驱动线程
_REAL_THREAD = threading.Thread

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest_parameter_grid as pg_api
from app.backtest.parameter_grid import (
    GridExperiment,
    GridScenarioResult,
    ParameterGridExperimentStore,
)
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService


class _ImmediateThread:
    """同步执行 target, 替换 threading.Thread 使实验在请求内完成。"""

    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class _FrozenThread:
    """记录 target 但不执行, 模拟线程尚未被调度 (resume 返回前线程一定没跑)。"""

    def __init__(self, target, daemon=None):
        self.target = target
        self.started = False

    def start(self):
        self.started = True


class _FakeStrategyEngine:
    def get(self, strategy_id: str):
        if strategy_id == "missing":
            raise ValueError("unknown strategy: missing")
        return SimpleNamespace(
            id=strategy_id,
            execution_backend="polars_expr",
            source="builtin",
            ephemeral=False,
            meta={
                "asset_types": ["stock"],
                "params": [{"id": "vol_ratio_min", "type": "float", "default": 1.5, "min": 0.5, "max": 5.0}],
            },
        )


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(pg_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = _FakeStrategyEngine()
    return TestClient(app)


def _persist_experiment(store: ParameterGridExperimentStore, *, status: str, strategy_id: str = "macd"):
    """落盘一个带检查点 (1/2 scenario 已完成) 的实验。"""
    cfg = StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=["600000.SH"],
        start=date(2024, 1, 1),
        end=date(2024, 6, 1),
    )
    base_config = {
        **StrategyBacktestService._config_to_dict(cfg),
        # 未来版本可能写入的未知字段 → 旧代码必须能读 (过滤)
        "future_field": "x",
    }
    exp = GridExperiment(
        experiment_id="pg-abcd00000001",
        config_hash="h1",
        strategy_id=strategy_id,
        objective="sharpe",
        base_config=base_config,
        grid={"vol_ratio_min": [1.0, 2.0]},
        requested_count=2,
        scenario_count=2,
        max_scenarios=24,
        truncated=False,
        status=status,
        scenarios=[
            GridScenarioResult(
                scenario_id="s0000",
                params={"vol_ratio_min": 1.0},
                stats={"sharpe": 1.0, "total_return": 0.1, "max_drawdown": -0.05},
                score=1.0,
                rank=1,
            ),
        ],
        best_scenario_id="s0000",
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
        completed=1,
        total=2,
    )
    store.save(exp)
    return exp


def test_resume_missing_experiment_404(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/backtest/parameter-grid/pg-0bad00000001/resume")
    assert resp.status_code == 404
    assert "实验不存在" in resp.json()["detail"]


def test_resume_rejects_cancelled(tmp_path):
    """cancelled 绝不可当 interrupted 续跑。"""
    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="cancelled")
    resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
    assert resp.status_code == 409
    assert "cancelled" in resp.json()["detail"]
    # 磁盘状态未被改动
    assert store.load("pg-abcd00000001").status == "cancelled"


def test_resume_rejects_completed(tmp_path):
    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="completed")
    resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
    assert resp.status_code == 409


def test_resume_rejects_running(tmp_path):
    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="running")
    resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
    assert resp.status_code == 409


def test_resume_rejects_unknown_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(pg_api, "_get_engine", lambda _req: object())
    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="interrupted", strategy_id="missing")
    resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
    assert resp.status_code == 400
    assert "unknown strategy" in resp.json()["detail"]


def test_resume_already_running_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(pg_api, "_get_engine", lambda _req: object())
    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="interrupted")

    job = pg_api._GridJob("pg-abcd00000001", "h1")
    pg_api._grid_jobs["pg-abcd00000001"] = job
    try:
        resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_running"
    finally:
        pg_api._grid_jobs.pop("pg-abcd00000001", None)


def test_resume_rebuilds_from_disk_and_returns_resumed(tmp_path, monkeypatch):
    """interrupted 实验 resume: 从 base_config+grid 重建, existing 传给 run_grid。"""
    captured: dict = {}

    def _fake_run_grid(**kwargs):
        captured.update(kwargs)
        exp = kwargs["existing"]
        # 返回新对象模拟 run_grid 的 replace(existing, ...): 不改端点闭包里的检查点快照
        done = replace(
            exp,
            status="completed",
            completed=2,
            scenarios=[
                *exp.scenarios,
                GridScenarioResult(
                    scenario_id="s0001",
                    params={"vol_ratio_min": 2.0},
                    stats={"sharpe": 2.0, "total_return": 0.2, "max_drawdown": -0.05},
                    score=2.0,
                    rank=1,
                ),
            ],
        )
        kwargs["store"].save(done)
        return done

    monkeypatch.setattr(pg_api, "run_grid", _fake_run_grid)

    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="interrupted")

    try:
        resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "resumed"
        assert body["experiment_id"] == "pg-abcd00000001"
        assert body["scenario_count"] == 2
        assert body["completed"] == 1

        # base_config 重建: 只填已知字段, 未知字段被过滤, iso 日期已解析
        bc = captured["base_config"]
        assert isinstance(bc, StrategyBacktestConfig)
        assert bc.strategy_id == "macd"
        assert bc.start == date(2024, 1, 1)
        assert bc.end == date(2024, 6, 1)
        assert bc.symbols == ["600000.SH"]
        assert not hasattr(bc, "future_field")

        # existing 为磁盘实验; scenarios 全量重建且 idx 对齐
        assert captured["existing"].experiment_id == "pg-abcd00000001"
        assert captured["experiment_id"] == "pg-abcd00000001"
        assert captured["config_hash"] == "h1"
        assert [s.params["vol_ratio_min"] for s in captured["scenarios"]] == [1.0, 2.0]
        assert [f"s{i:04d}" for i in range(len(captured["scenarios"]))] == ["s0000", "s0001"]
        assert captured["ng"].scenario_count == 2
        assert captured["ng"].grid == {"vol_ratio_min": [1.0, 2.0]}
    finally:
        pg_api._grid_jobs.pop("pg-abcd00000001", None)


def test_resume_failed_status_also_allowed(tmp_path, monkeypatch):
    """failed 实验同样允许从检查点续跑 (已完成 scenario 不重跑)。"""
    monkeypatch.setattr(pg_api, "_get_engine", lambda _req: object())
    monkeypatch.setattr(pg_api.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(pg_api, "run_grid", lambda **kw: kw["existing"])

    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="failed")

    try:
        resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "resumed"
    finally:
        pg_api._grid_jobs.pop("pg-abcd00000001", None)


def test_resume_marks_disk_running_before_thread_starts(tmp_path, monkeypatch):
    """认领锁内先把磁盘标 running: 线程未跑, resume 返回后 GET 已能看到 running。"""
    monkeypatch.setattr(pg_api, "_get_engine", lambda _req: object())
    monkeypatch.setattr(pg_api.threading, "Thread", _FrozenThread)

    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="interrupted")

    try:
        resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "resumed"

        # 线程被冻结根本没跑, 但磁盘已是 running (重启视角下不会仍是 interrupted)
        assert store.load("pg-abcd00000001").status == "running"
        detail = client.get("/api/backtest/parameter-grid/pg-abcd00000001")
        assert detail.status_code == 200
        assert detail.json()["status"] == "running"
    finally:
        pg_api._grid_jobs.pop("pg-abcd00000001", None)


def test_resume_concurrent_claims_only_once(tmp_path, monkeypatch):
    """并发 resume 同一实验: 只有一个请求真正认领并起线程, 其余 already_running。"""
    monkeypatch.setattr(pg_api, "_get_engine", lambda _req: object())

    starts: list[int] = []
    starts_lock = threading.Lock()

    class _CountingFrozenThread(_FrozenThread):
        def start(self):
            with starts_lock:
                starts.append(1)

    monkeypatch.setattr(pg_api.threading, "Thread", _CountingFrozenThread)

    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="interrupted")

    barrier = threading.Barrier(4)
    responses: list = []

    def _resume():
        barrier.wait()
        responses.append(client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume"))

    threads = [_REAL_THREAD(target=_resume) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(responses) == 4
        statuses = sorted(r.json()["status"] for r in responses)
        assert statuses == ["already_running", "already_running", "already_running", "resumed"]
        # 只起了一次线程, 磁盘只被认领一次
        assert len(starts) == 1
        assert store.load("pg-abcd00000001").status == "running"
    finally:
        pg_api._grid_jobs.pop("pg-abcd00000001", None)


def test_run_grid_exception_persists_failed(tmp_path, monkeypatch):
    """工作线程异常: 内存 job.error + 磁盘 status=failed (含 error), GET 可见 failed。"""
    monkeypatch.setattr(pg_api, "_get_engine", lambda _req: object())
    monkeypatch.setattr(pg_api.threading, "Thread", _ImmediateThread)

    def _boom(**kwargs):
        raise RuntimeError("grid exploded")

    monkeypatch.setattr(pg_api, "run_grid", _boom)

    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="interrupted")

    try:
        resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
        assert resp.status_code == 200

        disk = store.load("pg-abcd00000001")
        assert disk.status == "failed"
        assert "grid exploded" in disk.error

        job = pg_api._grid_jobs["pg-abcd00000001"]
        assert job.done is True
        assert job.error == "grid exploded"

        detail = client.get("/api/backtest/parameter-grid/pg-abcd00000001")
        assert detail.json()["status"] == "failed"
    finally:
        pg_api._grid_jobs.pop("pg-abcd00000001", None)


def test_run_grid_exception_keeps_cancelled(tmp_path, monkeypatch):
    """线程异常落盘 failed 时, 已被 cancel 的实验不被覆盖 (cancelled 不复活)。"""
    monkeypatch.setattr(pg_api, "_get_engine", lambda _req: object())

    created: list[_FrozenThread] = []

    class _CapturingFrozenThread(_FrozenThread):
        def __init__(self, target, daemon=None):
            super().__init__(target, daemon)
            created.append(self)

    def _boom(**kwargs):
        raise RuntimeError("late boom")

    monkeypatch.setattr(pg_api.threading, "Thread", _CapturingFrozenThread)
    monkeypatch.setattr(pg_api, "run_grid", _boom)

    client = _client(tmp_path)
    store = ParameterGridExperimentStore(tmp_path)
    _persist_experiment(store, status="interrupted")

    try:
        resp = client.post("/api/backtest/parameter-grid/pg-abcd00000001/resume")
        assert resp.status_code == 200
        assert store.load("pg-abcd00000001").status == "running"

        # 认领后、线程真正执行前, 用户点了 cancel (磁盘落 cancelled)
        exp = store.load("pg-abcd00000001")
        exp.status = "cancelled"
        store.save(exp)

        # 手动执行线程体: run_grid 异常, failed 落盘不得覆盖 cancelled
        created[0].target()
        assert store.load("pg-abcd00000001").status == "cancelled"

        job = pg_api._grid_jobs["pg-abcd00000001"]
        assert job.done is True
        assert job.error == "late boom"
    finally:
        pg_api._grid_jobs.pop("pg-abcd00000001", None)
