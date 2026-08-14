"""metrics 单元测试 — 回测绩效 / 风险指标。

覆盖验收项 (任务要求边界): 空、全亏、无交易、常量净值、损益极端值、
确定性 bootstrap seed。所有断言基于解析恒等式或手算值, 无随机量
(bootstrap 除外, 但用固定 seed 验证可复现)。

来源: ``../Vibe-Trading/agent/src/skills/{performance-attribution,
quant-statistics}/SKILL.md`` (纯 numpy 重写)。
"""

import math

import numpy as np

from app.backtest import metrics as mt

ok = "ok"
insuff = "insufficient_data"


# ---------------------------------------------------------------------------
# downside_deviation
# ---------------------------------------------------------------------------


def test_downside_deviation_mixed():
    # shortfall = [0, -0.05, -0.02]; dd = sqrt((0+0.0025+0.0004)/3) = 0.031091
    val = mt.downside_deviation([0.10, -0.05, -0.02])
    assert math.isclose(val, 0.031091, abs_tol=1e-5)


def test_downside_deviation_all_above_threshold_is_zero():
    assert mt.downside_deviation([0.1, 0.2, 0.05]) == 0.0


def test_downside_deviation_empty_is_none():
    assert mt.downside_deviation([]) is None
    assert mt.downside_deviation(None) is None


def test_downside_deviation_drops_nonfinite():
    # nan/inf 被剔除后只剩 [0.1, -0.1]
    assert math.isclose(mt.downside_deviation([0.1, float("nan"), -0.1, float("inf")]),
                        math.sqrt((0.0 + 0.01) / 2.0), abs_tol=1e-12)


# ---------------------------------------------------------------------------
# sortino
# ---------------------------------------------------------------------------


def test_sortino_analytic():
    # returns=[0.2,-0.1], rf=0, P=252, MAR=0:
    # ann_excess = 0.05*252 = 12.6 ; dd = sqrt(0.01/2) ; ann_dd = dd*sqrt(252)
    # sortino = 12.6 / (sqrt(0.005)*sqrt(252)) ≈ 11.2241
    val = mt.sortino_ratio([0.2, -0.1], periods_per_year=252, risk_free=0.0)
    assert math.isclose(val, 11.2241, abs_tol=1e-3)


def test_sortino_no_downside_is_none():
    # 全正收益 → 无下行风险 → None
    assert mt.sortino_ratio([0.1, 0.2, 0.05]) is None


def test_sortino_constant_returns_is_none():
    # 常量净值: 每期等值正收益, 无下行
    assert mt.sortino_ratio([0.05, 0.05, 0.05]) is None


def test_sortino_empty_is_none():
    assert mt.sortino_ratio([]) is None


def test_sortino_threshold_overrides_mar():
    # MAR(门槛) 只影响下行偏差分母; 抬高 MAR → 分母变大 → Sortino 变小
    # (分子仍用 risk_free, 故保持为正)
    base = mt.sortino_ratio([0.1, -0.05, 0.2, -0.1], threshold=0.0)
    high = mt.sortino_ratio([0.1, -0.05, 0.2, -0.1], threshold=0.5)
    assert base is not None and high is not None
    assert high < base


# ---------------------------------------------------------------------------
# omega
# ---------------------------------------------------------------------------


def test_omega_analytic():
    # gains=0.3, losses=0.2 → 1.5
    assert math.isclose(mt.omega_ratio([0.1, 0.2, -0.05, -0.15]), 1.5)


def test_omega_all_gains_is_none():
    assert mt.omega_ratio([0.1, 0.2, 0.3]) is None


def test_omega_all_losses_is_zero():
    assert mt.omega_ratio([-0.1, -0.2, -0.3]) == 0.0


def test_omega_empty_is_none():
    assert mt.omega_ratio([]) is None


def test_omega_threshold_balanced():
    # 全部恰在门槛上/下各半, 等量 → 1.0
    assert math.isclose(mt.omega_ratio([0.1, -0.1], threshold=0.0), 1.0)


# ---------------------------------------------------------------------------
# tail_ratio
# ---------------------------------------------------------------------------


def test_tail_ratio_right_heavier_gt_one():
    # 正向异常值拉高 95 分位
    val = mt.tail_ratio([-0.01, -0.01, 0.0, 0.01, 0.5])
    assert val is not None and val > 1.0


def test_tail_ratio_constant_nonzero_is_one():
    # 常量非零序列: p5 == p95 != 0 → 比值 1
    assert mt.tail_ratio([0.05, 0.05, 0.05]) == 1.0


def test_tail_ratio_constant_zero_is_none():
    assert mt.tail_ratio([0.0, 0.0, 0.0]) is None


def test_tail_ratio_empty_is_none():
    assert mt.tail_ratio([]) is None
    assert mt.tail_ratio([0.1]) is None  # 样本不足


# ---------------------------------------------------------------------------
# max_drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_persistent_decline():
    # 从 1.0 跌到 0.81 → -0.19 (含起点)
    assert math.isclose(mt.max_drawdown([-0.1, -0.1]), -0.19, abs_tol=1e-9)


def test_max_drawdown_known_valley():
    assert math.isclose(mt.max_drawdown([0.1, -0.2, 0.1]), -0.2, abs_tol=1e-9)


def test_max_drawdown_all_positive_is_zero():
    assert mt.max_drawdown([0.1, 0.2, 0.05]) == 0.0


def test_max_drawdown_extreme_single_period_clamped():
    # r=-1.5 使 wealth 翻负 → 钳到全损 -1.0
    assert mt.max_drawdown([-1.5]) == -1.0


def test_max_drawdown_empty_is_none():
    assert mt.max_drawdown([]) is None


# ---------------------------------------------------------------------------
# calmar
# ---------------------------------------------------------------------------


def test_calmar_analytic():
    # returns=[0.5,-0.25,0.5], P=3 (年化因子=1):
    # total=0.6875; mdd=-0.25; ann=(1.6875)^(3/3)-1=0.6875; calmar=0.6875/0.25=2.75
    val = mt.calmar_ratio([0.5, -0.25, 0.5], periods_per_year=3)
    assert math.isclose(val, 2.75, abs_tol=1e-9)


def test_calmar_no_drawdown_is_none():
    assert mt.calmar_ratio([0.1, 0.2]) is None


def test_calmar_empty_is_none():
    assert mt.calmar_ratio([]) is None


# ---------------------------------------------------------------------------
# ulcer_index
# ---------------------------------------------------------------------------


def test_ulcer_index_analytic():
    # dd=[0,0,-0.2,-0.12] (含起点); UI=sqrt((0+0+0.04+0.0144)/4)=sqrt(0.0136)=0.116619
    val = mt.ulcer_index([0.1, -0.2, 0.1])
    assert math.isclose(val, 0.116619, abs_tol=1e-5)


def test_ulcer_index_flat_is_zero():
    assert mt.ulcer_index([0.05, 0.05, 0.05]) == 0.0


def test_ulcer_index_empty_is_none():
    assert mt.ulcer_index([]) is None


# ---------------------------------------------------------------------------
# value_at_risk / conditional_value_at_risk
# ---------------------------------------------------------------------------


def test_var_matches_numpy_percentile():
    arr = [-0.1, -0.05, 0.0, 0.05, 0.1]
    assert math.isclose(mt.value_at_risk(arr, alpha=0.2), float(np.percentile(arr, 20)))


def test_var_is_loss_negative():
    assert mt.value_at_risk([-0.1, -0.05, 0.0, 0.05, 0.1], alpha=0.2) < 0.0


def test_var_invalid_alpha_is_none():
    assert mt.value_at_risk([0.1, 0.2], alpha=0.0) is None
    assert mt.value_at_risk([0.1, 0.2], alpha=1.0) is None
    assert mt.value_at_risk([0.1, 0.2], alpha=1.5) is None


def test_var_empty_is_none():
    assert mt.value_at_risk([]) is None


def test_cvar_analytic():
    # alpha=0.4 → VaR=percentile(40)=-0.02; tail=[-0.1,-0.05]; cvar=-0.075
    val = mt.conditional_value_at_risk([-0.1, -0.05, 0.0, 0.05, 0.1], alpha=0.4)
    assert math.isclose(val, -0.075, abs_tol=1e-9)


def test_cvar_le_var():
    # CVaR (更糟尾部均值) <= VaR
    arr = [-0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2]
    var = mt.value_at_risk(arr, alpha=0.25)
    cvar = mt.conditional_value_at_risk(arr, alpha=0.25)
    assert cvar <= var


def test_cvar_empty_is_none():
    assert mt.conditional_value_at_risk([]) is None


# ---------------------------------------------------------------------------
# profit_factor / payoff_ratio / expectancy (交易类)
# ---------------------------------------------------------------------------


def test_profit_factor_analytic():
    # gains=0.3, losses=0.2 → 1.5
    assert math.isclose(mt.profit_factor([0.1, 0.2, -0.05, -0.15]), 1.5)


def test_profit_factor_all_wins_is_none():
    assert mt.profit_factor([0.1, 0.2, 0.3]) is None


def test_profit_factor_all_losses_is_zero():
    assert mt.profit_factor([-0.1, -0.2, -0.3]) == 0.0


def test_profit_factor_empty_is_none():
    assert mt.profit_factor([]) is None


def test_profit_factor_extreme_values():
    # 极端盈亏: 一笔巨赚 + 一笔小亏
    val = mt.profit_factor([1000.0, -0.01])
    assert math.isclose(val, 100000.0)


def test_payoff_ratio_analytic():
    # avg_win=0.15, avg_loss=0.1 → 1.5
    assert math.isclose(mt.payoff_ratio([0.1, 0.2, -0.05, -0.15]), 1.5)


def test_payoff_ratio_no_wins_is_none():
    assert mt.payoff_ratio([-0.1, -0.2]) is None


def test_payoff_ratio_no_losses_is_none():
    assert mt.payoff_ratio([0.1, 0.2]) is None


def test_payoff_ratio_empty_is_none():
    assert mt.payoff_ratio([]) is None


def test_expectancy_equals_mean():
    assert math.isclose(mt.expectancy([0.1, -0.05, 0.2]), np.mean([0.1, -0.05, 0.2]))


def test_expectancy_all_losses_negative():
    assert mt.expectancy([-0.1, -0.2, -0.3]) < 0.0


def test_expectancy_empty_is_none():
    assert mt.expectancy([]) is None


# ---------------------------------------------------------------------------
# win_loss_streak
# ---------------------------------------------------------------------------


def test_win_loss_streak_analytic():
    res = mt.win_loss_streak([1, -1, 1, 1, -1, -1, -1])
    assert res == {"max_win_streak": 2, "max_loss_streak": 3, "n_wins": 3, "n_losses": 4}


def test_win_loss_streak_zero_breaks_streak():
    # 0 视为平局, 中断连胜
    res = mt.win_loss_streak([1, 1, 0, 1, 1, 1])
    assert res["max_win_streak"] == 3
    assert res["n_wins"] == 5
    assert res["n_losses"] == 0


def test_win_loss_streak_empty():
    res = mt.win_loss_streak([])
    assert res == {"max_win_streak": 0, "max_loss_streak": 0, "n_wins": 0, "n_losses": 0}


def test_win_loss_streak_all_losses():
    res = mt.win_loss_streak([-1, -2, -3])
    assert res["max_loss_streak"] == 3
    assert res["n_losses"] == 3
    assert res["max_win_streak"] == 0


# ---------------------------------------------------------------------------
# trade_duration_stats / exposure
# ---------------------------------------------------------------------------


def test_trade_duration_stats_analytic():
    res = mt.trade_duration_stats([1, 2, 3, 4])
    assert math.isclose(res["avg"], 2.5)
    assert math.isclose(res["median"], 2.5)
    assert res["min"] == 1.0
    assert res["max"] == 4.0
    assert res["n"] == 4


def test_trade_duration_stats_empty():
    res = mt.trade_duration_stats([])
    assert res == {"avg": None, "median": None, "min": None, "max": None, "n": 0}


def test_exposure_binary_time_in_market():
    assert math.isclose(mt.exposure([1, 1, 0, 1, 0]), 0.6)


def test_exposure_weights_avg_capital():
    assert math.isclose(mt.exposure([0.5, 0.25, 1.0]), np.mean([0.5, 0.25, 1.0]))


def test_exposure_empty_is_none():
    assert mt.exposure([]) is None


# ---------------------------------------------------------------------------
# bootstrap_confidence_interval
# ---------------------------------------------------------------------------


def test_bootstrap_deterministic_same_seed():
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    a = mt.bootstrap_confidence_interval(data, statistic=np.mean, n_bootstrap=500, seed=123)
    b = mt.bootstrap_confidence_interval(data, statistic=np.mean, n_bootstrap=500, seed=123)
    assert a["status"] == ok
    assert a == b  # 完全可复现
    assert a["n_bootstrap"] == 500


def test_bootstrap_mean_ci_brackets_point():
    data = list(range(20))
    res = mt.bootstrap_confidence_interval(data, statistic=np.mean, n_bootstrap=1000, seed=7)
    assert res["status"] == ok
    assert math.isclose(res["point_estimate"], np.mean(data))
    # 均值的 bootstrap 均值近似无偏
    assert math.isclose(res["bootstrap_mean"], np.mean(data), abs_tol=0.5)
    assert res["ci_lower"] < res["ci_upper"]
    assert res["ci_lower"] <= res["point_estimate"] <= res["ci_upper"]


def test_bootstrap_insufficient_data():
    assert mt.bootstrap_confidence_interval([1.0], seed=0)["status"] == insuff
    assert mt.bootstrap_confidence_interval([], seed=0)["status"] == insuff
    assert mt.bootstrap_confidence_interval(None, seed=0)["status"] == insuff


def test_bootstrap_invalid_confidence():
    res = mt.bootstrap_confidence_interval([1, 2, 3], confidence=1.5, seed=0)
    assert res["status"] == insuff


def test_bootstrap_handles_failing_statistic():
    # 统计量恒抛异常 → 全部重采样 nan → insufficient
    def boom(_):
        raise ZeroDivisionError

    res = mt.bootstrap_confidence_interval([1.0, 2.0, 3.0], statistic=boom, n_bootstrap=50, seed=1)
    assert res["status"] == insuff
    assert res["point_estimate"] is None


def test_bootstrap_custom_statistic_median():
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    res = mt.bootstrap_confidence_interval(data, statistic=np.median, n_bootstrap=300, seed=42)
    assert res["status"] == ok
    assert res["point_estimate"] == 3.0


def test_bootstrap_different_seeds_differ_in_general():
    data = list(np.random.default_rng(99).normal(size=30))
    a = mt.bootstrap_confidence_interval(data, statistic=np.mean, n_bootstrap=1000, seed=1)
    b = mt.bootstrap_confidence_interval(data, statistic=np.mean, n_bootstrap=1000, seed=2)
    # 不同 seed 的 CI 端点几乎不可能完全相同
    assert not (math.isclose(a["ci_lower"], b["ci_lower"]) and math.isclose(a["ci_upper"], b["ci_upper"]))


# ---------------------------------------------------------------------------
# performance_metrics (聚合入口)
# ---------------------------------------------------------------------------


def test_performance_metrics_all_inputs():
    res = mt.performance_metrics(
        returns=[0.1, -0.2, 0.15, 0.05, -0.1],
        pnls=[0.1, 0.2, -0.05, -0.15, 0.3],
        durations=[2, 5, 1, 3, 4],
        positions=[1, 1, 0, 1, 1],
        periods_per_year=252,
    )
    assert res["status"] == ok
    # 路径类键齐全
    for key in ("sortino", "omega", "tail_ratio", "max_drawdown",
                "calmar", "ulcer_index", "value_at_risk",
                "conditional_value_at_risk", "downside_deviation"):
        assert key in res
    # 交易类键齐全
    for key in ("profit_factor", "payoff_ratio", "expectancy", "win_loss_streak"):
        assert key in res
    assert "trade_duration" in res and "exposure" in res
    # 子结构正确
    assert res["win_loss_streak"]["n_wins"] == 3
    assert res["trade_duration"]["n"] == 5
    assert math.isclose(res["exposure"], 0.8)


def test_performance_metrics_returns_only():
    res = mt.performance_metrics(returns=[0.1, -0.05, 0.2])
    assert res["status"] == ok
    assert "sortino" in res
    assert "profit_factor" not in res
    assert "trade_duration" not in res


def test_performance_metrics_pnls_only():
    res = mt.performance_metrics(pnls=[0.1, -0.05, 0.2])
    assert res["status"] == ok
    assert "profit_factor" in res
    assert "sortino" not in res


def test_performance_metrics_no_trades_empty():
    res = mt.performance_metrics(returns=[0.1, -0.05], pnls=[], durations=None)
    assert res["status"] == ok
    assert "sortino" in res
    assert "profit_factor" not in res  # 空交易 → 不产出交易类键


def test_performance_metrics_all_empty_is_insufficient():
    assert mt.performance_metrics()["status"] == insuff
    assert mt.performance_metrics(returns=[], pnls=[])["status"] == insuff


def test_performance_metrics_constant_equity():
    # 常量净值: 无下行 / 无回撤 → sortino、calmar 为 None; ulcer 0; mdd 0
    res = mt.performance_metrics(returns=[0.05, 0.05, 0.05])
    assert res["status"] == ok
    assert res["sortino"] is None
    assert res["calmar"] is None
    assert res["ulcer_index"] == 0.0
    assert res["max_drawdown"] == 0.0


def test_performance_metrics_all_losses_trades():
    # 全亏交易: profit_factor=0, payoff None, expectancy<0
    res = mt.performance_metrics(pnls=[-0.1, -0.2, -0.3])
    assert res["status"] == ok
    assert res["profit_factor"] == 0.0
    assert res["payoff_ratio"] is None
    assert res["expectancy"] < 0.0
    assert res["win_loss_streak"]["max_loss_streak"] == 3


def test_performance_metrics_extreme_pnl():
    # 损益极端值: 一笔巨亏 + 几笔小赚, 不崩
    res = mt.performance_metrics(pnls=[0.01, 0.02, 0.01, -1000.0])
    assert res["status"] == ok
    # profit_factor 非常小 (接近 0)
    assert res["profit_factor"] is not None and res["profit_factor"] < 1e-4
    assert res["expectancy"] < 0.0


# ---------------------------------------------------------------------------
# 非有限值 / None 输入清理 (fail-soft)
# ---------------------------------------------------------------------------


def test_functions_handle_none_input():
    assert mt.sortino_ratio(None) is None
    assert mt.omega_ratio(None) is None
    assert mt.max_drawdown(None) is None
    assert mt.profit_factor(None) is None
    assert mt.expectancy(None) is None
    assert mt.value_at_risk(None) is None


def test_nonfinite_inputs_dropped():
    # 混入 nan/inf 不应破坏计算
    arr = [0.1, float("nan"), -0.1, float("inf")]
    assert mt.profit_factor(arr) is not None  # 0.1 vs -0.1 → 1.0
    assert math.isclose(mt.profit_factor(arr), 1.0)
