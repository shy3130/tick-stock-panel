"""严格 Walk-Forward (训练→冻结→OOS) 纯函数/服务桩测试。

关键契约:
- OOS 指标不参与选参 (结构上 select 只接收训练结果);
- 折间 OOS 不重叠、train 先于 OOS 且 expanding;
- 参数在训练选参后冻结, OOS 只跑冻结参数一次;
- OOS 拼接曲线逐折归一链式相乘、日期升序;
- 短区间返回空折 + 结构化 warning, 不伪造;
- 候选空间有界: baseline + 2*max_perturbed_params。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.backtest import robustness as rb


@dataclass
class _Run:
    stats: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)
    error: str | None = None


def _perturbation_cases():
    return rb.parameter_perturbations(
        [{"id": "lookback", "type": "int", "default": 20, "min": 5, "max": 60, "step": 5}],
        {"lookback": 20},
        fraction=0.1,
    )


# ── 折计划几何 ────────────────────────────────────────────


def test_fold_plan_expanding_train_and_non_overlapping_oos():
    plan = rb.walk_forward_fold_plan(date(2024, 1, 1), date(2024, 12, 31), 4)
    assert len(plan) == 4
    start = date(2024, 1, 1)
    for spec in plan:
        assert spec["train_start"] == start          # expanding: 训练窗都从区间起点开始
        assert spec["train_end"] < spec["oos_start"]  # 训练严格先于 OOS
        assert spec["oos_start"] <= spec["oos_end"]
    for prev, cur in zip(plan, plan[1:]):
        assert prev["oos_end"] < cur["oos_start"]     # OOS 窗互不重叠
        assert prev["train_end"] < cur["train_end"]   # 训练窗逐折扩张
    assert plan[-1]["oos_end"] == date(2024, 12, 31)  # OOS 铺满区间尾部


def test_fold_plan_reduces_fold_count_when_range_tight():
    plan = rb.walk_forward_fold_plan(date(2024, 1, 1), date(2024, 4, 9), 4)
    assert len(plan) == 2  # 100 天放不下 4 折, 自动收缩
    for spec in plan:
        assert (spec["oos_end"] - spec["oos_start"]).days + 1 >= rb.WALK_FORWARD_MIN_OOS_DAYS


def test_fold_plan_short_range_returns_empty_not_fabricated():
    assert rb.walk_forward_fold_plan(date(2024, 1, 1), date(2024, 2, 1), 4) == []
    empty = rb.empty_walk_forward(4)
    assert empty["folds"] == []
    assert empty["stitched_curve"] == []
    assert empty["warning"] is not None and "30" in empty["warning"]
    assert empty["n_candidates"] == 0


def test_fold_plan_caps_requested_folds():
    assert len(rb.walk_forward_fold_plan(date(2020, 1, 1), date(2024, 12, 31), 99)) == rb.WALK_FORWARD_MAX_FOLDS


# ── 候选空间与选择 ────────────────────────────────────────


def test_candidates_bounded_baseline_first():
    cases = _perturbation_cases()
    assert len(cases) <= 2 * 6  # max_perturbed_params=6 → 上下各一
    candidates = rb.walk_forward_candidates({"lookback": 20}, cases[: 2 * 6])
    assert len(candidates) == 1 + len(cases)  # ≤ baseline + 2*max_perturbed_params
    assert candidates[0]["label"] == "baseline"
    assert candidates[0]["params"] == {"lookback": 20}
    assert {c["label"] for c in candidates[1:]} == {"lookback=15", "lookback=25"}


def test_select_candidate_train_metric_only_with_stable_tie_break():
    # 平局: 先出现者 (baseline) 稳定胜出
    assert rb.select_walk_forward_candidate([
        _Run(stats={"sharpe": 1.0}), _Run(stats={"sharpe": 1.0}),
    ]) == 0
    # 训练指标更高者胜, 与出现顺序无关
    assert rb.select_walk_forward_candidate([
        _Run(stats={"sharpe": 1.0}), _Run(stats={"sharpe": 2.5}), _Run(stats={"sharpe": 2.0}),
    ]) == 1
    # None/NaN/非数值不可选; 全部不可计算时确定性回退 baseline
    assert rb.select_walk_forward_candidate([
        _Run(stats={"sharpe": None}), _Run(stats={"sharpe": float("nan")}), _Run(stats={}),
    ]) == 0


# ── 编排: 无泄漏 / 冻结 / 拼接 ─────────────────────────────


_PLAN = rb.walk_forward_fold_plan(date(2024, 1, 1), date(2024, 6, 30), 2)


def _make_run_fn(*, baseline_train_sharpe, perturbed_train_sharpe, baseline_oos, perturbed_oos):
    """按窗口角色 (train/OOS) 与参数返回确定性结果的回测桩。

    train 窗都从 2024-01-01 开始, OOS 窗更晚 — 以此区分角色。
    """
    calls: list[tuple[date, date, dict]] = []

    def run_fn(start: date, end: date, params: dict):
        calls.append((start, end, dict(params)))
        perturbed = params.get("lookback") != 20
        if start == date(2024, 1, 1):
            sharpe = perturbed_train_sharpe if perturbed else baseline_train_sharpe
            return _Run(stats={"sharpe": sharpe, "total_return": 0.1})
        stats, curve = (perturbed_oos if perturbed else baseline_oos)
        return _Run(stats=dict(stats), equity_curve=[dict(p) for p in curve])

    return run_fn, calls


def test_oos_performance_cannot_influence_selection():
    """OOS 大幅更优但训练更差的候选不得被选中 — OOS 数据不进入选择。"""
    run_fn, calls = _make_run_fn(
        baseline_train_sharpe=2.0,
        perturbed_train_sharpe=1.0,
        baseline_oos=({"sharpe": -5.0, "total_return": -0.3}, [{"date": "2024-03-04", "value": 1.0}, {"date": "2024-03-05", "value": 1.05}]),
        perturbed_oos=({"sharpe": 9.9, "total_return": 0.9}, [{"date": "2024-03-04", "value": 1.0}, {"date": "2024-03-05", "value": 1.5}]),
    )
    candidates = rb.walk_forward_candidates({"lookback": 20}, _perturbation_cases())
    result = rb.run_walk_forward(_PLAN, candidates, run_fn, base_params={"lookback": 20})

    assert len(result["folds"]) == 2
    assert all(fold["selected_label"] == "baseline" for fold in result["folds"])
    # 每折恰好: n_candidates 次训练 + 1 次冻结参数 OOS
    assert len(calls) == 2 * (len(candidates) + 1)


def test_train_winner_params_frozen_into_oos_and_degradation():
    """训练期胜出的扰动参数必须原样冻结进 OOS 运行, 并计算退化。"""
    run_fn, calls = _make_run_fn(
        baseline_train_sharpe=2.0,
        perturbed_train_sharpe=3.0,
        baseline_oos=({"sharpe": 1.0, "total_return": 0.05}, []),
        perturbed_oos=({"sharpe": 1.5, "total_return": 0.08}, [{"date": "2024-03-04", "value": 1.0}, {"date": "2024-03-05", "value": 1.02}]),
    )
    candidates = rb.walk_forward_candidates({"lookback": 20}, _perturbation_cases())
    result = rb.run_walk_forward(_PLAN, candidates, run_fn, base_params={"lookback": 20})

    # lookback=15 与 lookback=25 训练 Sharpe 并列 3.0: 稳定 tie-break 取先出现的 15
    assert all(fold["selected_label"] == "lookback=15" for fold in result["folds"])
    oos_calls = [c for c in calls if c[0] != date(2024, 1, 1)]
    assert oos_calls and all(c[2] == {"lookback": 15} for c in oos_calls)
    for fold in result["folds"]:
        assert fold["selected_params"] == {"lookback": 15}
        assert fold["degradation"] == 1.5  # 训练 3.0 − OOS 1.5
    assert result["param_drift"]["n_distinct_param_sets"] == 1
    assert result["param_drift"]["params"] == {"lookback": [15, 15]}
    assert result["warning"] is None


def test_run_walk_forward_deterministic_with_same_stub():
    def factory():
        return _make_run_fn(
            baseline_train_sharpe=2.0,
            perturbed_train_sharpe=1.0,
            baseline_oos=({"sharpe": 0.5, "total_return": 0.02}, [{"date": "2024-03-04", "value": 1.0}, {"date": "2024-03-05", "value": 1.01}]),
            perturbed_oos=({"sharpe": 0.4, "total_return": 0.01}, [{"date": "2024-03-04", "value": 1.0}]),
        )

    candidates = rb.walk_forward_candidates({"lookback": 20}, _perturbation_cases())
    a = rb.run_walk_forward(_PLAN, candidates, factory()[0], base_params={"lookback": 20})
    b = rb.run_walk_forward(_PLAN, candidates, factory()[0], base_params={"lookback": 20})
    assert a == b


def test_fold_output_order_train_before_oos_no_oos_overlap():
    run_fn, _ = _make_run_fn(
        baseline_train_sharpe=2.0,
        perturbed_train_sharpe=1.0,
        baseline_oos=({"sharpe": 0.5, "total_return": 0.02}, [{"date": "2024-03-04", "value": 1.0}, {"date": "2024-03-05", "value": 1.01}]),
        perturbed_oos=({"sharpe": 0.4, "total_return": 0.01}, []),
    )
    candidates = rb.walk_forward_candidates({"lookback": 20}, _perturbation_cases())
    result = rb.run_walk_forward(_PLAN, candidates, run_fn, base_params={"lookback": 20})
    folds = result["folds"]
    for fold in folds:
        assert fold["train_start"] < fold["train_end"] < fold["oos_start"] <= fold["oos_end"]
    for prev, cur in zip(folds, folds[1:]):
        assert prev["oos_end"] < cur["oos_start"]


def test_oos_error_fold_is_reported_and_skipped_in_stitch():
    def run_fn(start: date, end: date, params: dict):
        if start == date(2024, 1, 1):
            return _Run(stats={"sharpe": 1.0, "total_return": 0.1})
        if start >= date(2024, 4, 30):
            return _Run(error="insufficient_data")
        return _Run(
            stats={"sharpe": 0.5, "total_return": 0.02},
            equity_curve=[{"date": "2024-03-04", "value": 1.0}, {"date": "2024-03-05", "value": 1.02}],
        )

    result = rb.run_walk_forward(
        _PLAN,
        rb.walk_forward_candidates({"lookback": 20}, []),
        run_fn,
        base_params={"lookback": 20},
    )
    errors = [f["error"] for f in result["folds"]]
    assert errors == [None, "insufficient_data"]
    assert all(f["oos_curve"] == [] for f in result["folds"] if f["error"])
    assert len(result["stitched_curve"]) == 2  # 仅无错折参与拼接
    assert result["summary"]["n_folds"] == 1


# ── 拼接曲线 ──────────────────────────────────────────────


def test_stitch_oos_curves_chains_folds_and_skips_invalid():
    stitched = rb.stitch_oos_curves([
        [{"date": "2024-01-02", "value": 1.0}, {"date": "2024-01-03", "value": 1.1}],
        [],  # 空折跳过
        [{"date": "2024-02-01", "value": float("nan")}],  # 非有限首点跳过
        [{"date": "2024-03-01", "value": 2.0}, {"date": "2024-03-04", "value": 2.2}, {"date": "2024-03-05", "value": 2.0}],
    ])
    assert [p["value"] for p in stitched] == [1.0, 1.1, 1.1, 1.21, 1.1]
    assert [p["date"] for p in stitched] == sorted(p["date"] for p in stitched)


def test_stitched_summary_values():
    stitched = rb.stitch_oos_curves([
        [{"date": "2024-01-02", "value": 1.0}, {"date": "2024-01-03", "value": 1.1}],
        [{"date": "2024-02-01", "value": 1.0}, {"date": "2024-02-02", "value": 1.1}],
    ])
    summary = rb.walk_forward_oos_summary(
        [{"oos_stats": {"total_return": 0.1}, "degradation": 0.5, "error": None},
         {"oos_stats": {"total_return": -0.05}, "degradation": 1.5, "error": None}],
        stitched,
    )
    assert summary["n_folds"] == 2
    assert summary["positive_return_folds"] == 1
    assert summary["positive_fold_ratio"] == 0.5
    assert summary["worst_fold_return"] == -0.05
    assert summary["mean_degradation"] == 1.0
    assert abs(summary["oos_total_return"] - 0.2100) < 1e-6
    assert summary["oos_sharpe"] is not None
    assert summary["oos_max_drawdown"] == 0.0  # 单调上行无回撤


def test_summary_all_none_when_no_data():
    summary = rb.walk_forward_oos_summary([], [])
    assert summary["n_folds"] == 0
    assert summary["positive_fold_ratio"] is None
    assert summary["mean_degradation"] is None
    assert summary["oos_sharpe"] is None
    assert summary["oos_total_return"] is None
