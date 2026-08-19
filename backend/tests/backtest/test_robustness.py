import numpy as np

from app.backtest import robustness as rb
from app.api.backtest import _derive_random_seed


def test_bootstrap_ci_deterministic_with_seed():
    rets = np.random.default_rng(1).normal(0.001, 0.01, 300)
    a = rb.bootstrap_sharpe_ci(rets, n_boot=200, seed=7)
    b = rb.bootstrap_sharpe_ci(rets, n_boot=200, seed=7)
    assert a == b
    assert a["ci_low"] < a["sharpe"] < a["ci_high"]


def test_permutation_pvalue_low_for_strong_signal():
    rets = np.random.default_rng(3).normal(0.002, 0.005, 400)
    assert rb.mc_permutation_pvalue(rets, n_perm=500, seed=3)["p_value"] < 0.05


def test_derived_seed_is_stable_and_tracks_data_version():
    payload = {"strategy_id": "momentum", "params": {"lookback": 20}}
    seed = _derive_random_seed(payload, "generation-a")
    assert seed == _derive_random_seed(payload, "generation-a")
    assert 0 <= seed <= (1 << 63) - 1
    assert seed != _derive_random_seed(payload, "generation-b")
    assert seed != _derive_random_seed(
        {"strategy_id": "momentum", "params": {"lookback": 60}},
        "generation-a",
    )


def test_exit_reason_breakdown():
    rows = rb.exit_reason_breakdown([
        {"exit_reason": "stop_loss", "pnl_pct": -5.0},
        {"exit_reason": "stop_loss", "pnl_pct": -4.0},
        {"exit_reason": "signal", "pnl_pct": 8.0},
        {"exit_reason": None, "pnl_pct": 1.0},
    ])
    by = {r["exit_reason"]: r for r in rows}
    assert by["stop_loss"]["n"] == 2
    assert by["stop_loss"]["win_rate"] == 0.0
    assert by["signal"]["avg_pnl_pct"] == 8.0
    assert by["(none)"]["n"] == 1


def test_segment_stability_summary_dispersion():
    s = rb.segment_stability_summary([{"stats": {"sharpe": 1.0}}, {"stats": {"sharpe": 1.2}}, {"stats": {"sharpe": -0.3}}])
    assert s["n_folds"] == 3
    assert s["positive_folds"] == 2
    assert abs(s["mean"] - 0.6333) < 1e-3


def test_segment_stability_summary_skips_none_and_nonfinite_metrics():
    """stats.sharpe 存在但值为 None (策略服务真实会产生) 时不得 TypeError, 也不得被 NaN 污染。"""
    s = rb.segment_stability_summary([
        {"stats": {"sharpe": None}},
        {"stats": {"sharpe": 1.0}},
        {"stats": {"sharpe": float("nan")}},
        {"stats": {"sharpe": -0.5}},
        {"stats": {}},
        {"stats": None},
    ])
    assert s["n_folds"] == 2
    assert s["mean"] == 0.25
    assert s["worst"] == -0.5
    assert s["positive_folds"] == 1


def test_segment_stability_summary_all_uncomputable_metrics_fall_back():
    """全部折的指标都不可计算时返回确定兜底摘要, 不抛异常。"""
    s = rb.segment_stability_summary([{"stats": {"sharpe": None}}, {"stats": {}}])
    assert s == {"metric": "sharpe", "n_folds": 0, "mean": 0.0, "std": 0.0, "worst": 0.0, "positive_folds": 0}


def test_parameter_perturbations_respect_types_bounds_and_defaults():
    cases = rb.parameter_perturbations(
        [
            {"id": "lookback", "label": "回看", "type": "int", "default": 20, "min": 10, "max": 21, "step": 5},
            {"id": "threshold", "type": "float", "default": 0.0, "min": -0.2, "max": 0.2, "step": 0.05},
            {"id": "enabled", "type": "bool", "default": True},
        ],
        {"lookback": 20},
        fraction=0.1,
    )
    assert [(case["param"], case["direction"], case["value"]) for case in cases] == [
        ("lookback", "down", 15),
        ("lookback", "up", 21),
        ("threshold", "down", -0.05),
        ("threshold", "up", 0.05),
    ]


def test_parameter_perturbations_bound_number_of_parameters():
    cases = rb.parameter_perturbations(
        [
            {"id": "first", "type": "float", "default": 1.0},
            {"id": "second", "type": "float", "default": 2.0},
        ],
        None,
        max_params=1,
    )
    assert {case["param"] for case in cases} == {"first"}


def test_walk_forward_planned_executions_formula():
    """每折 = 候选训练次数 + 1 次冻结后 OOS; 0 折时无任何额外执行。"""
    assert rb.walk_forward_planned_executions(4, 5) == 24
    assert rb.walk_forward_planned_executions(1, 1) == 2  # 仅 baseline: 1 次训练 + 1 次 OOS
    assert rb.walk_forward_planned_executions(0, 13) == 0
    assert rb.walk_forward_planned_executions(6, 3) == 24


def test_cap_walk_forward_candidates_budget_is_mathematically_bounded():
    """全部合法折数 × 任意候选数下, 截断后执行数恒 ≤ 预算 (上界证明的可执行验证)。"""
    candidates = [{"label": f"c{i}", "params": {"p": i}, "perturbed_param": None} for i in range(40)]
    for n_folds in range(1, rb.WALK_FORWARD_MAX_FOLDS + 1):
        for requested in (0, 1, 2, 3, 5, 13, 17, 40):
            out = rb.cap_walk_forward_candidates(candidates[:requested], n_folds)
            assert out["requested_candidates"] == requested
            assert out["effective_candidates"] == len(out["candidates"])
            assert out["planned_executions"] == rb.walk_forward_planned_executions(
                n_folds, out["effective_candidates"]
            )
            assert out["planned_executions"] <= rb.WALK_FORWARD_MAX_EXTRA_EXECUTIONS
            if out["effective_candidates"] > 0:
                assert out["candidates"][0]["label"] == "c0"  # baseline 永不丢弃
                assert out["candidates"] == candidates[: out["effective_candidates"]]  # 前序截取


def test_cap_walk_forward_candidates_truncates_deterministically_with_warning():
    """13 候选 × 4 折: 截断为 24//4-1=5 个, warning 说明预算, baseline 最先。"""
    base = {"label": "baseline", "params": {}, "perturbed_param": None}
    cases = [{"label": f"x={i}", "params": {"x": i}, "perturbed_param": "x"} for i in range(12)]
    out = rb.cap_walk_forward_candidates([base, *cases], 4)
    assert out["requested_candidates"] == 13
    assert out["effective_candidates"] == 5
    assert out["planned_executions"] == 24
    assert out["candidates"][0]["label"] == "baseline"
    assert [c["label"] for c in out["candidates"][1:]] == [f"x={i}" for i in range(4)]
    assert out["warning"] is not None and "24" in out["warning"] and "截断" in out["warning"]
    assert out["max_executions"] == rb.WALK_FORWARD_MAX_EXTRA_EXECUTIONS == 24


def test_cap_walk_forward_candidates_no_trim_when_within_budget():
    """3 折 × 5 候选 = 18 ≤ 24: 不截断、不产生 warning。"""
    candidates = [{"label": f"c{i}", "params": {}, "perturbed_param": None} for i in range(5)]
    out = rb.cap_walk_forward_candidates(candidates, 3)
    assert out["candidates"] == candidates
    assert out["effective_candidates"] == out["requested_candidates"] == 5
    assert out["warning"] is None


def test_cap_walk_forward_candidates_zero_folds_never_trims():
    """无折可跑 (短区间) 时无执行也无需截断, 由调用方输出区间 warning。"""
    candidates = [{"label": "baseline", "params": {}, "perturbed_param": None}]
    out = rb.cap_walk_forward_candidates(candidates, 0)
    assert out["candidates"] == candidates
    assert out["planned_executions"] == 0
    assert out["warning"] is None


def test_disabled_walk_forward_returns_structured_empty_block():
    """未启用: enabled=False + 明确 warning + 空折, 不伪造任何结果。"""
    out = rb.disabled_walk_forward()
    assert out["enabled"] is False
    assert out["n_candidates"] == 0
    assert out["requested_candidates"] == 0
    assert out["effective_candidates"] == 0
    assert out["max_executions"] == rb.WALK_FORWARD_MAX_EXTRA_EXECUTIONS
    assert out["folds"] == []
    assert out["stitched_curve"] == []
    assert out["summary"]["n_folds"] == 0
    assert out["param_drift"]["n_distinct_param_sets"] == 0
    assert "walk_forward_enabled" in out["warning"]
