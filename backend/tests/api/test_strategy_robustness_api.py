"""/strategy/robustness 响应契约隔离测试 — segment_stability 改名 + 严格 walk_forward 结构。

stub 掉真实回测引擎与数据快照, 全部落 tmp_path, 不读行情不写真实 data/。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api


@dataclass
class _WindowResult:
    """按窗口日期生成确定性单调净值曲线的回测结果桩。"""

    run_id: str = "robrun0001"
    config: dict = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: {
        "sharpe": 1.0, "total_return": 0.05, "annual_return": 0.12,
        "max_drawdown": -0.02, "win_rate": 0.5, "n_trades": 3,
    })
    equity_curve: list = field(default_factory=list)
    drawdown_curve: list = field(default_factory=list)
    benchmark_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    per_symbol_stats: list = field(default_factory=list)
    strategy_info: dict = field(default_factory=lambda: {"id": "macd", "name": "MACD", "source": "builtin"})
    elapsed_ms: float = 1.0
    error: str | None = None


class _WindowAwareService:
    def __init__(self, *args, **kwargs):
        pass

    def run(self, cfg, progress_cb=None, cancel_event=None, **kwargs):
        days = (cfg.end - cfg.start).days
        step = days / 1000.0
        curve = [
            {"date": cfg.start.isoformat(), "value": 1.0},
            {"date": cfg.start.isoformat(), "value": 1.0 + step},
            {"date": cfg.end.isoformat(), "value": 1.0 + 2 * step},
        ]
        return _WindowResult(equity_curve=curve)


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        enriched_latest_date=lambda: None,
    )
    app.state.strategy_engine = SimpleNamespace()
    app.state.backtest_engine = object()
    return TestClient(app)


def test_robustness_response_segment_stability_and_walk_forward(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _WindowAwareService)
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: None)
    client = _client(tmp_path)

    resp = client.post("/api/backtest/strategy/robustness", json={
        "strategy_id": "macd",
        "start": "2026-01-01",
        "end": "2026-06-30",
        "n_folds": 4,
        "walk_forward_enabled": True,
        "parameter_perturbation": False,
        "bootstrap": False,
        "mc_permutation": False,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 改名 cutover: 同参数顺序切段由 segment_stability 承接, 不再有旧键语义
    assert "segment_stability" in body
    seg = body["segment_stability"]
    assert len(seg["folds"]) == 4
    assert set(seg["folds"][0]) == {"start", "end", "stats", "error"}
    assert seg["folds"][0]["start"] == "2026-01-01"
    assert seg["folds"][-1]["end"] == "2026-06-30"
    assert seg["summary"]["n_folds"] == 4
    # 严格 walk-forward: 每折训练先于 OOS、参数冻结、OOS 拼接曲线起点归一
    wf = body["walk_forward"]
    assert wf["enabled"] is True
    assert wf["n_candidates"] == 1  # parameter_perturbation=False → 仅 baseline
    assert wf["requested_candidates"] == 1
    assert wf["effective_candidates"] == 1
    assert wf["max_executions"] == 24  # rb.WALK_FORWARD_MAX_EXTRA_EXECUTIONS
    assert len(wf["folds"]) == 4
    fold = wf["folds"][0]
    assert fold["train_start"] == "2026-01-01"
    assert fold["train_start"] < fold["train_end"] < fold["oos_start"] <= fold["oos_end"]
    assert fold["selected_label"] == "baseline"
    assert fold["n_candidates"] == 1
    assert fold["degradation"] == 0.0  # 桩: 训练与 OOS Sharpe 相同
    oos_windows = [(f["oos_start"], f["oos_end"]) for f in wf["folds"]]
    for (_s1, e1), (s2, _e2) in zip(oos_windows, oos_windows[1:]):
        assert e1 < s2  # OOS 窗互不重叠
    assert len(wf["stitched_curve"]) == 4 * 3
    assert wf["stitched_curve"][0]["value"] == 1.0
    assert wf["stitched_curve"][-1]["value"] > 1.0
    assert wf["warning"] is None
    assert wf["summary"]["n_folds"] == 4
    assert wf["summary"]["positive_fold_ratio"] == 1.0
    assert wf["param_drift"]["n_distinct_param_sets"] == 1

    # 持久化 Run 与响应共用同一 robustness 结构
    run_resp = client.get("/api/backtest/runs/robrun0001")
    assert run_resp.status_code == 200
    persisted = run_resp.json()["stats"]["robustness"]
    assert persisted["segment_stability"]["summary"]["n_folds"] == 4
    assert persisted["walk_forward"]["stitched_curve"] == wf["stitched_curve"]


def test_robustness_walk_forward_short_range_warns_instead_of_failing(
    tmp_path: Path, monkeypatch
):
    """45 天 + 1 折: 分段稳定性可切 (45≥30), 但 Walk-Forward 放不下
    初始训练 + ≥30 天 OOS → 200 + 结构化 warning + 空折, 不伪造。"""
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _WindowAwareService)
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: None)
    client = _client(tmp_path)
    resp = client.post("/api/backtest/strategy/robustness", json={
        "strategy_id": "macd",
        "start": "2026-01-01",
        "end": "2026-02-14",  # 45 天
        "n_folds": 1,
        "walk_forward_enabled": True,
        "parameter_perturbation": False,
        "bootstrap": False,
        "mc_permutation": False,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["segment_stability"]["folds"]) == 1  # 分段稳定性不受影响
    wf = body["walk_forward"]
    assert wf["folds"] == []
    assert wf["stitched_curve"] == []
    assert wf["warning"] is not None and "30" in wf["warning"]
    assert any("walk_forward" in str(w) for w in body.get("warnings") or [])


def test_robustness_walk_forward_minimal_single_fold(
    tmp_path: Path, monkeypatch
):
    """60 天 + 2 折请求: 自动收缩为 1 折 (初始训练 30 天 + OOS 30 天)。"""
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _WindowAwareService)
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: None)
    client = _client(tmp_path)

    resp = client.post("/api/backtest/strategy/robustness", json={
        "strategy_id": "macd",
        "start": "2026-01-01",
        "end": "2026-03-01",  # 60 天
        "n_folds": 2,
        "walk_forward_enabled": True,
        "parameter_perturbation": False,
        "bootstrap": False,
        "mc_permutation": False,
    })
    body = resp.json()
    assert body["walk_forward"]["warning"] is None
    assert len(body["walk_forward"]["folds"]) == 1


_COUNTING_SERVICE_INSTANCES: list["_CountingService"] = []


class _CountingService(_WindowAwareService):
    """记录每次回测调用的窗口与参数 — 用于证明执行次数的精确上界。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[dict] = []
        _COUNTING_SERVICE_INSTANCES.append(self)

    def run(self, cfg, progress_cb=None, cancel_event=None, **kwargs):
        self.calls.append({
            "start": cfg.start.isoformat(),
            "end": cfg.end.isoformat(),
            "params": dict(cfg.params or {}),
        })
        return super().run(cfg, progress_cb=progress_cb, cancel_event=cancel_event, **kwargs)


_NUMERIC_PARAM_SPECS = [
    {"id": "fast", "type": "int", "default": 12, "min": 2, "max": 60, "step": 1},
    {"id": "slow", "type": "int", "default": 26, "min": 5, "max": 120, "step": 1},
    {"id": "signal", "type": "int", "default": 9, "min": 2, "max": 40, "step": 1},
    {"id": "exit_days", "type": "int", "default": 5, "min": 1, "max": 30, "step": 1},
    {"id": "vol_ratio", "type": "float", "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    {"id": "trail_pct", "type": "float", "default": 2.0, "min": 0.5, "max": 10.0, "step": 0.1},
]


def _counting_client(tmp_path: Path, *, numeric_params: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        enriched_latest_date=lambda: None,
    )
    if numeric_params:
        app.state.strategy_engine = SimpleNamespace(
            get=lambda _sid: SimpleNamespace(meta={"params": _NUMERIC_PARAM_SPECS})
        )
    else:
        app.state.strategy_engine = SimpleNamespace()
    app.state.backtest_engine = object()
    return TestClient(app)


def _last_service_calls() -> list[dict]:
    assert _COUNTING_SERVICE_INSTANCES, "服务未被实例化"
    return _COUNTING_SERVICE_INSTANCES[-1].calls


def test_robustness_walk_forward_disabled_by_default_runs_no_extra_backtests(
    tmp_path: Path, monkeypatch
):
    """默认 (不发 walk_forward_enabled): 执行成本与旧版分段+扰动完全一致 —
    1 次全周期 + n_folds 次分段 + 扰动 case 次, Walk-Forward 额外执行为 0;
    响应仍带 walk_forward 结构化块, enabled=false + warning, 不伪造结果。"""
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _CountingService)
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: None)
    client = _counting_client(tmp_path)

    resp = client.post("/api/backtest/strategy/robustness", json={
        "strategy_id": "macd",
        "start": "2026-01-01",
        "end": "2026-06-30",
        "n_folds": 4,
        "parameter_perturbation": False,
        "bootstrap": False,
        "mc_permutation": False,
    })
    assert resp.status_code == 200, resp.text
    # 1 次全周期 + 4 次分段, 无任何 Walk-Forward 额外执行
    assert len(_last_service_calls()) == 1 + 4
    body = resp.json()
    wf = body["walk_forward"]
    assert wf["enabled"] is False
    assert wf["folds"] == []
    assert wf["stitched_curve"] == []
    assert wf["n_candidates"] == 0
    assert "walk_forward_enabled" in wf["warning"]
    assert any("walk_forward" in str(w) for w in body.get("warnings") or [])
    # 持久化 Run 与响应共用同一结构化空块
    persisted = client.get("/api/backtest/runs/robrun0001").json()["stats"]["robustness"]
    assert persisted["walk_forward"]["enabled"] is False


def test_robustness_walk_forward_enabled_execution_budget_and_deterministic_truncation(
    tmp_path: Path, monkeypatch
):
    """启用 + 6 个数值参数: 请求 13 候选, 4 折预算 24//4-1=5 → 确定性截断为
    baseline + 前 4 个候选; Walk-Forward 额外执行 = 4 折 × (5+1) = 24 恰达上限。"""
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _CountingService)
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: None)
    client = _counting_client(tmp_path, numeric_params=True)

    resp = client.post("/api/backtest/strategy/robustness", json={
        "strategy_id": "macd",
        "start": "2026-01-01",
        "end": "2026-06-30",
        "n_folds": 4,
        "walk_forward_enabled": True,
        "parameter_perturbation": True,
        "max_perturbed_params": 6,
        "bootstrap": False,
        "mc_permutation": False,
        "params": {"fast": 12, "slow": 26, "signal": 9, "exit_days": 5, "vol_ratio": 1.5, "trail_pct": 2.0},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    wf = body["walk_forward"]
    assert wf["enabled"] is True
    assert wf["requested_candidates"] == 13  # baseline + 6 参数 × ±2
    assert wf["effective_candidates"] == 5
    assert wf["max_executions"] == 24
    assert len(wf["folds"]) == 4
    assert all(f["n_candidates"] == 5 for f in wf["folds"])
    assert wf["warning"] is not None and "截断" in wf["warning"] and "24" in wf["warning"]
    assert any("walk_forward" in str(w) for w in body.get("warnings") or [])
    # 扰动展示 12 个 case (与候选共用同一份邻域, 未被截断影响)
    assert len(body["parameter_perturbation"]["cases"]) == 12
    # 执行数精确可证: 1 全周期 + 4 分段 + 12 扰动 + 4×(5 训练 + 1 OOS) = 41
    assert len(_last_service_calls()) == 1 + 4 + 12 + 4 * (5 + 1)


def test_robustness_walk_forward_budget_holds_for_all_ui_fold_choices(
    tmp_path: Path, monkeypatch
):
    """UI 折数 3..6 全档位: 额外执行 = 折数 × (effective+1) ≤ 24 恒成立。"""
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _CountingService)
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: None)
    client = _counting_client(tmp_path, numeric_params=True)

    for n_folds in (3, 4, 5, 6):
        resp = client.post("/api/backtest/strategy/robustness", json={
            "strategy_id": "macd",
            "start": "2026-01-01",
            "end": "2026-06-30",
            "n_folds": n_folds,
            "walk_forward_enabled": True,
            "parameter_perturbation": False,
            "bootstrap": False,
            "mc_permutation": False,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        wf = body["walk_forward"]
        assert wf["enabled"] is True
        # 区间放不下请求折数时计划自动收缩 (181 天 + 6 折 → 5 折), 预算按实际折数计算
        actual_folds = len(wf["folds"])
        expected_folds = 5 if n_folds == 6 else n_folds
        assert actual_folds == expected_folds
        planned = actual_folds * (wf["effective_candidates"] + 1)
        assert planned <= wf["max_executions"] == 24
        # 全周期 + n_folds 分段 + 每折 (候选训练 + 1 次 OOS), 不多不少
        assert len(_last_service_calls()) == 1 + n_folds + planned


def test_robustness_rejects_candidate_execution_before_running_engine(
    tmp_path: Path, monkeypatch
):
    """退出事件采样曲线没有日频收益语义，不能进入稳健性时间序列分析。"""
    constructed: list[object] = []

    class _MustNotRunService:
        def __init__(self, *args, **kwargs):
            constructed.append(object())
            raise AssertionError("候选执行不得构造稳健性回测服务")

    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _MustNotRunService)
    client = _client(tmp_path)

    resp = client.post("/api/backtest/strategy/robustness", json={
        "strategy_id": "macd",
        "start": "2026-01-01",
        "end": "2026-06-30",
        "mode": "full",
    })

    assert resp.status_code == 422, resp.text
    assert "全量独立候选执行" in resp.json()["detail"]
    assert constructed == []
