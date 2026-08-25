"""F15 组合级回测测试 — 已固化 Run 日频净值的事后加权合成。

函数级直接构造 BacktestRun 验证数值口径（daily/monthly/none 三种再平衡
的闭式解、权重归一化、贡献、相关性矩阵、交集不足与非法 run 拒绝）；
API 级用 TestClient 挂临时 run_store，覆盖 404/422 与响应契约。全程不
触发行情。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.backtest.portfolio_combine import (
    PortfolioCombineError,
    combine_run_equities,
)
from app.backtest.run_store import BacktestRun, BacktestRunStore, RunSubject


# ── 公共构造 ────────────────────────────────────────────────

def _dates(start: date, n: int) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _geometric(dates: list[str], daily_ret: float, base: float = 100.0) -> list[dict]:
    """常数日收益净值曲线（首日为 base）。"""
    return [
        {"date": d, "value": round(base * (1.0 + daily_ret) ** k, 6)}
        for k, d in enumerate(dates)
    ]


def _piecewise(dates: list[str], rets_by_month: dict[str, float], base: float = 100.0) -> list[dict]:
    """按月份分段常数日收益净值曲线。

    某日的收益属于该日所属月份（即 d_k → d_{k+1} 的收益按 d_{k+1} 的月份
    取分段值），与实现里"月边界日在月初重置后计入新月收益"的语义一致。
    """
    values: list[dict] = []
    v = base
    for k, d in enumerate(dates):
        values.append({"date": d, "value": round(v, 6)})
        if k + 1 < len(dates):
            v *= 1.0 + rets_by_month[dates[k + 1][:7]]
    return values


def _make_run(
    run_id: str,
    equity: list[dict],
    *,
    kind: str = "strategy",
    stats: dict | None = None,
    label: str = "",
    subject_name: str = "",
) -> BacktestRun:
    return BacktestRun(
        run_id=run_id,
        kind=kind,
        created_at="2024-03-01T00:00:00+00:00",
        subject=RunSubject(id=run_id, name=subject_name or run_id, hash="h1"),
        config={"strategy_id": run_id},
        stats=stats or {},
        equity_curve=equity,
        label=label,
    )


def _two_runs(n: int = 22):
    """A: 每日 +10%; B: 每日 -5%; 日期跨 1~2 月 (1 月 11 天 + 2 月 11 天)。"""
    jan = _dates(date(2024, 1, 2), 11)
    feb = _dates(date(2024, 2, 1), 11)
    dates = jan + feb
    run_a = _make_run("runA", _geometric(dates, 0.10), label="动量")
    run_b = _make_run("runB", _geometric(dates, -0.05))
    return dates, run_a, run_b


# ── 函数级: 再平衡三模式 ────────────────────────────────────

def test_equal_weight_daily_combine_matches_closed_form():
    """等权 daily: 组合日收益 = Σ w_i·r_i，净值 = (1.025)^k 闭式一致。"""
    dates, run_a, run_b = _two_runs()
    result = combine_run_equities([(run_a, 1.0), (run_b, 1.0)], "daily")
    expected = [1.025 ** k for k in range(len(dates))]
    got = [row["value"] for row in result["equity_curve"]]
    assert len(got) == len(dates)
    assert got[0] == pytest.approx(1.0)
    for g, e in zip(got, expected):
        assert g == pytest.approx(e, abs=1e-5)
    assert result["stats"]["total_return"] == pytest.approx(1.025 ** 21 - 1, abs=1e-5)
    assert result["overlap_days"] == len(dates)
    assert result["rebalance"] == "daily"
    # 成分各自收益: 首末净值比
    assert result["items"][0]["total_return"] == pytest.approx(1.1 ** 21 - 1, rel=1e-6)
    assert result["items"][1]["total_return"] == pytest.approx(0.95 ** 21 - 1, rel=1e-6)
    # 口径提示必须在 warnings 里
    assert any("事后加权合成" in w and "非共享资金池撮合" in w for w in result["warnings"])


def test_monthly_rebalance_resets_weights_at_month_start():
    """monthly: 每月首个共同交易日重置权重，段内漂移；与闭式分段复利一致。"""
    jan = _dates(date(2024, 1, 2), 11)
    feb = _dates(date(2024, 2, 1), 11)
    dates = jan + feb
    # 1 月: A +10% / B -5%; 2 月: A -2% / B +30% (幅度不对称, 与 none 区分)
    run_a = _make_run("runA", _piecewise(dates, {"2024-01": 0.10, "2024-02": -0.02}))
    run_b = _make_run("runB", _piecewise(dates, {"2024-01": -0.05, "2024-02": 0.30}))
    result = combine_run_equities([(run_a, 1.0), (run_b, 1.0)], "monthly")

    m1 = 0.5 * 1.1 ** 10 + 0.5 * 0.95 ** 10
    m2 = 0.5 * 0.98 ** 10 + 0.5 * 1.30 ** 10
    expected = [1.0]
    # 1 月内逐日漂移 (无重置); 2 月首个共同交易日开盘重置后按目标权重逐日漂移
    for k in range(1, 11):
        expected.append(0.5 * 1.1 ** k + 0.5 * 0.95 ** k)
    for k in range(1, 12):
        expected.append(m1 * (0.5 * 0.98 ** k + 0.5 * 1.30 ** k))
    got = [row["value"] for row in result["equity_curve"]]
    assert len(got) == len(expected) == 22
    for g, e in zip(got, expected):
        assert g == pytest.approx(e, abs=1e-4)
    # 终值 = 月初重置后的分段复利; 与 none (买入持有漂移) 明显不同
    none_final = 0.5 * 1.1 ** 10 * 0.98 ** 10 + 0.5 * 0.95 ** 10 * 1.30 ** 10
    assert got[-1] != pytest.approx(none_final, rel=1e-6)
    # monthly 终值也可写成 m1 * m2_shift, 其中 2 月段含 11 个收益 (首日即新月)
    assert got[-1] == pytest.approx(m1 * (0.5 * 0.98 ** 11 + 0.5 * 1.30 ** 11), abs=1e-4)


def test_none_drift_equals_buy_and_hold_weighted_growth():
    """none: 买入持有加权漂移, 净值恒等于 Σ w_i·g_i(t)。"""
    dates, run_a, run_b = _two_runs()
    result = combine_run_equities([(run_a, 1.0), (run_b, 1.0)], "none")
    got = [row["value"] for row in result["equity_curve"]]
    for k, g in enumerate(got):
        expected = 0.5 * 1.1 ** k + 0.5 * 0.95 ** k
        assert g == pytest.approx(expected, abs=1e-5)


def test_weight_normalization_notes_raw_sum():
    """权重原始和 ≠ 1 → 归一化并注明原值; weight_raw 保留原始权重。"""
    dates, run_a, run_b = _two_runs()
    result = combine_run_equities([(run_a, 2.0), (run_b, 2.0)], "daily")
    assert [it["weight"] for it in result["items"]] == pytest.approx([0.5, 0.5])
    assert [it["weight_raw"] for it in result["items"]] == [2.0, 2.0]
    assert any("权重原始和为 4" in w for w in result["warnings"])
    # 等权 daily 数值与 weight=1 输入一致 (归一化不改变结果)
    baseline = combine_run_equities([(run_a, 1.0), (run_b, 1.0)], "daily")
    assert [r["value"] for r in result["equity_curve"]] == pytest.approx(
        [r["value"] for r in baseline["equity_curve"]]
    )


def test_contribution_sums_to_one_when_gain_nonzero():
    """贡献 = 各账户净值增量占组合总增量之比, 恰好求和为 1。"""
    jan = _dates(date(2024, 1, 2), 11)
    feb = _dates(date(2024, 2, 1), 11)
    dates = jan + feb
    run_a = _make_run("runA", _piecewise(dates, {"2024-01": 0.10, "2024-02": -0.02}))
    run_b = _make_run("runB", _piecewise(dates, {"2024-01": -0.05, "2024-02": 0.30}))
    result = combine_run_equities([(run_a, 1.0), (run_b, 3.0)], "monthly")
    contribs = [it["contribution"] for it in result["items"]]
    assert all(c is not None for c in contribs)
    assert sum(contribs) == pytest.approx(1.0, abs=1e-9)
    # 2 月 B 大涨且权重 0.75 → B 贡献应为正
    assert contribs[1] > 0


def test_correlation_matrix_symmetric_diagonal_one():
    """相关性矩阵对称、对角 1；与 np.corrcoef 逐对一致；零方差成分 → None。"""
    dates, _, _ = _two_runs()
    # 交替 ±4% 与其镜像: 严格负相关 (不做中途舍入, 避免噪声破坏恒定假设)
    r_a = [0.04, -0.04] * 11
    vals_a, v = [100.0], 100.0
    for r in r_a:
        v *= 1.0 + r
        vals_a.append(v)
    vals_b, v = [100.0], 100.0
    for r in r_a:
        v *= 1.0 - r
        vals_b.append(v)
    run_alt_a = _make_run("runA", [
        {"date": d, "value": x} for d, x in zip(dates, vals_a)
    ])
    run_alt_b = _make_run("runB", [
        {"date": d, "value": x} for d, x in zip(dates, vals_b)
    ])
    run_c = _make_run("runC", _geometric(dates, 0.0))  # 恒定净值 → 零方差
    result = combine_run_equities(
        [(run_alt_a, 1.0), (run_alt_b, 1.0), (run_c, 1.0)], "daily"
    )
    matrix = result["correlation_matrix"]
    assert matrix["run_ids"] == ["runA", "runB", "runC"]
    values = matrix["values"]
    for i in range(3):
        assert values[i][i] == 1.0
        for j in range(3):
            assert values[i][j] == pytest.approx(values[j][i])
    # 交替收益与其镜像完全负相关, 且与 np.corrcoef 逐对一致
    expected_ab = float(np.corrcoef(np.asarray(r_a), -np.asarray(r_a))[0, 1])
    assert values[0][1] == pytest.approx(expected_ab, abs=1e-9)
    assert values[0][1] == pytest.approx(-1.0, abs=1e-9)
    # 零方差对不伪造数值
    assert values[0][2] is None and values[2][1] is None


# ── 函数级: 拒绝路径 (fail-closed) ──────────────────────────

def test_overlap_below_20_days_rejected():
    """共同交易日 < 20 → 中文原因含计数。"""
    long_dates = _dates(date(2024, 1, 2), 35)
    run_a = _make_run("runA", _geometric(long_dates, 0.01))
    run_b = _make_run("runB", _geometric(long_dates[:10], 0.01))
    with pytest.raises(PortfolioCombineError) as exc:
        combine_run_equities([(run_a, 1.0), (run_b, 1.0)], "daily")
    assert "共同交易日仅 10 天" in str(exc.value)


def test_candidate_execution_run_rejected():
    """候选执行模式 run 无日频语义 → 拒绝并指出是哪个 run。"""
    dates, run_a, run_b = _two_runs()
    cand = _make_run(
        "runC", _geometric(dates, 0.01), stats={"full_kind": "candidate_execution"}
    )
    with pytest.raises(PortfolioCombineError) as exc:
        combine_run_equities([(run_a, 1.0), (cand, 1.0)], "daily")
    assert "runC" in str(exc.value) and "候选执行" in str(exc.value)


def test_factor_run_and_empty_curve_rejected():
    """因子 run / 净值点不足的 run → 拒绝并指出 id。"""
    dates, run_a, run_b = _two_runs()
    factor = _make_run("runF", _geometric(dates, 0.01), kind="factor")
    with pytest.raises(PortfolioCombineError) as exc:
        combine_run_equities([(run_a, 1.0), (factor, 1.0)], "daily")
    assert "runF" in str(exc.value) and "因子回测" in str(exc.value)

    empty = _make_run("runE", [])
    with pytest.raises(PortfolioCombineError) as exc:
        combine_run_equities([(run_a, 1.0), (empty, 1.0)], "daily")
    assert "runE" in str(exc.value)


# ── API 级 ──────────────────────────────────────────────────

@pytest.fixture()
def api_client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = SimpleNamespace()
    app.state.backtest_engine = object()
    return TestClient(app)


def _save_run(tmp_path: Path, run: BacktestRun) -> None:
    BacktestRunStore(tmp_path).save(run)


def test_api_combine_returns_curve_stats_and_warnings(api_client, tmp_path):
    """200: 合成净值/指标/成分/相关性齐备, warnings 含口径提示与归一化说明。"""
    dates, run_a, run_b = _two_runs()
    _save_run(tmp_path, run_a)
    _save_run(tmp_path, run_b)
    res = api_client.post("/api/backtest/portfolio-combine", json={
        "items": [{"run_id": "runA", "weight": 2}, {"run_id": "runB", "weight": 2}],
        "rebalance": "daily",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["equity_curve"][0]["value"] == pytest.approx(1.0)
    assert body["equity_curve"][-1]["value"] == pytest.approx(1.025 ** 21, abs=1e-4)
    assert body["overlap_days"] == len(dates)
    assert body["rebalance"] == "daily"
    for key in ("annual_return", "sharpe", "annual_volatility", "max_drawdown", "calmar"):
        assert key in body["stats"]
    assert body["stats"]["metric_context"]["return_frequency"] == "daily"
    assert len(body["items"]) == 2
    assert any("事后加权合成" in w for w in body["warnings"])
    assert any("权重原始和为 4" in w for w in body["warnings"])
    assert body["correlation_matrix"]["run_ids"] == ["runA", "runB"]


def test_api_missing_run_404_names_id(api_client, tmp_path):
    dates, run_a, _ = _two_runs()
    _save_run(tmp_path, run_a)
    res = api_client.post("/api/backtest/portfolio-combine", json={
        "items": [{"run_id": "runA", "weight": 1}, {"run_id": "ghost_run", "weight": 1}],
    })
    assert res.status_code == 404
    assert "ghost_run" in res.json()["detail"]


def test_api_items_out_of_range_422(api_client):
    one = [{"run_id": "runA", "weight": 1}]
    res = api_client.post("/api/backtest/portfolio-combine", json={"items": one})
    assert res.status_code == 422
    nine = [{"run_id": f"r{i}", "weight": 1} for i in range(9)]
    res = api_client.post("/api/backtest/portfolio-combine", json={"items": nine})
    assert res.status_code == 422


def test_api_duplicate_run_id_422(api_client, tmp_path):
    dates, run_a, run_b = _two_runs()
    _save_run(tmp_path, run_a)
    _save_run(tmp_path, run_b)
    res = api_client.post("/api/backtest/portfolio-combine", json={
        "items": [
            {"run_id": "runA", "weight": 1},
            {"run_id": "runA", "weight": 1},
        ],
    })
    assert res.status_code == 422
    assert "runA" in res.json()["detail"] and "重复" in res.json()["detail"]


def test_api_small_overlap_422(api_client, tmp_path):
    long_dates = _dates(date(2024, 1, 2), 35)
    _save_run(tmp_path, _make_run("runA", _geometric(long_dates, 0.01)))
    _save_run(tmp_path, _make_run("runB", _geometric(long_dates[:10], 0.01)))
    res = api_client.post("/api/backtest/portfolio-combine", json={
        "items": [{"run_id": "runA", "weight": 1}, {"run_id": "runB", "weight": 1}],
    })
    assert res.status_code == 422
    assert "共同交易日仅 10 天" in res.json()["detail"]


def test_api_candidate_run_422(api_client, tmp_path):
    dates, run_a, run_b = _two_runs()
    _save_run(tmp_path, run_a)
    _save_run(tmp_path, _make_run(
        "runC", _geometric(dates, 0.01), stats={"full_kind": "candidate_execution"}
    ))
    res = api_client.post("/api/backtest/portfolio-combine", json={
        "items": [{"run_id": "runA", "weight": 1}, {"run_id": "runC", "weight": 1}],
    })
    assert res.status_code == 422
    assert "runC" in res.json()["detail"] and "候选执行" in res.json()["detail"]


def test_api_zero_weight_422(api_client, tmp_path):
    dates, run_a, run_b = _two_runs()
    _save_run(tmp_path, run_a)
    _save_run(tmp_path, run_b)
    res = api_client.post("/api/backtest/portfolio-combine", json={
        "items": [{"run_id": "runA", "weight": 0}, {"run_id": "runB", "weight": 1}],
    })
    assert res.status_code == 422
