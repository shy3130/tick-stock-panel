"""F8 蒙特卡洛交易顺序重排 — monte_carlo_trade_shuffle 纯函数 + 端点契约测试。

口径: 交易级、顺序无关的重排诊断 (不模拟资金占用/并发持仓, 不是账户级 MC)。
API 桩沿用 test_strategy_robustness_api 的隔离方式 — stub 回测服务, 不读行情
不写真实 data/, 全部落 tmp_path。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.backtest import robustness as rb


def _alternating_returns(pairs: int = 30, up: float = 0.05, down: float = -0.04) -> np.ndarray:
    """+up/-down 交替: 原始顺序回撤很浅 (每对内 ~4%), 随机重排后回撤几乎必然更深。"""
    return np.tile([up, down], pairs)


# ----------------------------------------------------------------------
# 纯函数: monte_carlo_trade_shuffle
# ----------------------------------------------------------------------

def test_shuffle_deterministic_with_seed():
    """同 seed 两次调用结果逐字段一致 — 可复现是诊断口径的硬要求。"""
    rets = np.random.default_rng(7).normal(0.002, 0.03, 80)
    a = rb.monte_carlo_trade_shuffle(rets, n_sims=200, seed=42)
    b = rb.monte_carlo_trade_shuffle(rets, n_sims=200, seed=42)
    assert a == b


def test_all_positive_returns_prob_final_negative_zero_and_zero_dd():
    """收益全正: 任何重排终值都 > 0 且路径单调上行 → 概率 0、回撤分布全 0。"""
    rets = np.linspace(0.001, 0.02, 60)
    out = rb.monte_carlo_trade_shuffle(rets, n_sims=200, seed=42)
    assert out["prob_final_negative"] == 0.0
    dd = out["max_drawdown"]
    assert dd["p95"] == dd["p50"] == dd["mean"] == 0.0
    # 原始顺序也无回撤 → 没有模拟能比实际更差
    assert out["prob_max_dd_worse_than_actual"] == 0.0


def test_all_negative_returns_prob_final_negative_one():
    """收益全负: 任何重排终值都 < 0 → 概率 1, 且 p95 终值仍为负。"""
    rets = np.linspace(-0.02, -0.001, 60)
    out = rb.monte_carlo_trade_shuffle(rets, n_sims=200, seed=42)
    assert out["prob_final_negative"] == 1.0
    assert out["final_return"]["p95"] < 0.0


def test_quantiles_monotonic():
    """p05 ≤ p50 ≤ p95 对 final_return 与 max_drawdown 同时成立 (混合收益)。"""
    rets = np.random.default_rng(11).normal(0.001, 0.04, 120)
    out = rb.monte_carlo_trade_shuffle(rets, n_sims=500, seed=42)
    for block in (out["final_return"], out["max_drawdown"]):
        assert block["p05"] <= block["p50"] <= block["p95"]


def test_insufficient_trades_returns_none():
    """29 笔 (< 30) → None (fail-closed, 与 regime 桶样本不足置 None 同风格)。"""
    assert rb.monte_carlo_trade_shuffle(_alternating_returns(pairs=14)[:-1], seed=42) is None

def test_nonfinite_values_filtered_before_threshold():
    """NaN/inf 先剔除再数样本: 有效 18 (< 30) → None; 有效 30 ≥ 30 → 正常
    输出且 n_trades 只计有限值。"""
    rets = np.concatenate([_alternating_returns(pairs=9), np.full(11, np.nan)])
    assert rb.monte_carlo_trade_shuffle(rets, seed=42) is None
    rets_ok = np.concatenate([_alternating_returns(pairs=15), np.full(5, np.inf)])
    out = rb.monte_carlo_trade_shuffle(rets_ok, n_sims=200, seed=42)
    assert out is not None and out["n_trades"] == 30

def test_histogram_counts_sum_to_n_sims_with_20_bins():
    """dd 直方图: 计数之和 = n_sims (每个模拟都有落桶), 20 桶 21 条边。"""
    rets = np.random.default_rng(3).normal(0.0, 0.05, 100)
    out = rb.monte_carlo_trade_shuffle(rets, n_sims=400, seed=42)
    hist = out["dd_histogram"]
    assert len(hist["counts"]) == rb.MONTE_CARLO_DD_BINS == 20
    assert len(hist["bin_edges"]) == 21
    assert sum(hist["counts"]) == out["n_sims"] == 400
    assert all(e0 < e1 for e0, e1 in zip(hist["bin_edges"], hist["bin_edges"][1:]))


def test_prob_max_dd_worse_than_actual_in_unit_interval_and_wired_to_original_order():
    """概率 ∈ [0,1]; 且与按文档口径 (SeedSequence(seed).spawn 独立子流重排 +
    原始顺序同口径回撤) 的独立重算一致 — 验证 actual 确实取自原始顺序。"""
    rets = np.random.default_rng(5).normal(0.001, 0.04, 90)
    n_sims, seed = 200, 42
    out = rb.monte_carlo_trade_shuffle(rets, n_sims=n_sims, seed=seed)
    prob = out["prob_max_dd_worse_than_actual"]
    assert prob is not None and 0.0 <= prob <= 1.0

    def path_max_dd(values: np.ndarray) -> float:
        equity = np.cumprod(np.concatenate(([1.0], 1.0 + values)))
        peak = np.maximum.accumulate(equity)
        return float(np.max((peak - equity) / peak))

    actual = path_max_dd(rets)
    worse = 0
    for child in np.random.SeedSequence(seed).spawn(n_sims):
        shuffled = np.random.default_rng(child).permutation(rets)
        if path_max_dd(shuffled) > actual:
            worse += 1
    assert prob == round(worse / n_sims, 4)


def test_shallow_dd_ordering_makes_shuffle_almost_always_worse():
    """+5%/-4% 交替的原始顺序回撤极浅 (~4%): 随机重排几乎必然更深 →
    prob_max_dd_worse_than_actual ≈ 1 — 这是“顺序运气”问题要捕捉的信号。"""
    out = rb.monte_carlo_trade_shuffle(_alternating_returns(pairs=30), n_sims=400, seed=42)
    assert out["prob_max_dd_worse_than_actual"] > 0.99


# ----------------------------------------------------------------------
# API 契约: /strategy/robustness 响应含 monte_carlo 键
# ----------------------------------------------------------------------

@dataclass
class _WindowResult:
    """带确定交易列表的回测结果桩。"""

    run_id: str = "mcrun00001"
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


class _TradesStubService:
    """按 trades 参数生成确定性结果的桩服务。"""

    trades: list = []

    def __init__(self, *args, **kwargs):
        pass

    def run(self, cfg, progress_cb=None, cancel_event=None, **kwargs):
        curve = [
            {"date": cfg.start.isoformat(), "value": 1.0},
            {"date": cfg.start.isoformat(), "value": 1.01},
            {"date": cfg.end.isoformat(), "value": 1.02},
        ]
        return _WindowResult(equity_curve=curve, trades=list(self.trades))


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


_BODY = {
    "strategy_id": "macd",
    "start": "2026-01-01",
    "end": "2026-06-30",
    "n_folds": 1,
    "parameter_perturbation": False,
    "bootstrap": False,
    "mc_permutation": False,
}


def _post_robustness(tmp_path: Path, monkeypatch, trades: list, query: str = ""):
    _TradesStubService.trades = trades
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _TradesStubService)
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: None)
    client = _client(tmp_path)
    return client.post(f"/api/backtest/strategy/robustness{query}", json=_BODY)


def test_endpoint_response_contains_monte_carlo_block(tmp_path: Path, monkeypatch):
    """>= 30 笔成交: 响应含 monte_carlo 键, 默认 n_sims=1000, 结构完整。"""
    trades = [{"pnl_pct": v} for v in np.tile([0.02, -0.01], 20)]
    resp = _post_robustness(tmp_path, monkeypatch, trades)
    assert resp.status_code == 200, resp.text
    mc = resp.json()["monte_carlo"]
    assert mc["n_sims"] == 1000
    assert mc["n_trades"] == 40
    for key in ("final_return", "max_drawdown"):
        assert set(mc[key]) == {"p05", "p50", "p95", "mean"}
    assert 0.0 <= mc["prob_final_negative"] <= 1.0
    assert sum(mc["dd_histogram"]["counts"]) == 1000
    # 同一 robustness 结构并入持久化 Run 的 stats
    persisted = client_get_run(tmp_path, monkeypatch, trades, "mcrun00001")
    assert persisted["stats"]["robustness"]["monte_carlo"] == mc


def client_get_run(tmp_path: Path, monkeypatch, trades: list, run_id: str):
    """读取持久化 Run — 与首次请求共用同一 tmp store, 需重放同参请求。"""
    _TradesStubService.trades = trades
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _TradesStubService)
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: None)
    client = _client(tmp_path)
    run_resp = client.get(f"/api/backtest/runs/{run_id}")
    assert run_resp.status_code == 200, run_resp.text
    return run_resp.json()


def test_endpoint_mc_sims_query_param_clamped_to_validation(tmp_path: Path, monkeypatch):
    """mc_sims Query 参数生效 (150); 越界 (< 100) → 422 参数校验拦截。"""
    trades = [{"pnl_pct": v} for v in np.tile([0.02, -0.01], 20)]
    resp = _post_robustness(tmp_path, monkeypatch, trades, query="?mc_sims=150")
    assert resp.status_code == 200, resp.text
    assert resp.json()["monte_carlo"]["n_sims"] == 150
    assert sum(resp.json()["monte_carlo"]["dd_histogram"]["counts"]) == 150

    bad = _post_robustness(tmp_path, monkeypatch, trades, query="?mc_sims=99")
    assert bad.status_code == 422


def test_endpoint_insufficient_trades_returns_null_monte_carlo(tmp_path: Path, monkeypatch):
    """< 30 笔成交: monte_carlo 为 null (fail-closed), 其余块不受影响。"""
    trades = [{"pnl_pct": 0.01} for _ in range(12)]
    resp = _post_robustness(tmp_path, monkeypatch, trades)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["monte_carlo"] is None
    assert "segment_stability" in body
