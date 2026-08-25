"""quant_stats 单元测试 — ADF / 协整 / GARCH / Granger / VIF。

覆盖验收项: 平稳 vs 随机游走、协整构造序列、GARCH 非负方差、Granger 滞后关系、
VIF 共线、空 / 样本不足 fail-soft。

来源: ``../Vibe-Trading/agent/src/skills/quant-statistics/SKILL.md`` (纯 numpy 重写)。
"""

import math

import numpy as np
import pytest

from app.backtest import quant_stats as qs

# ---------------------------------------------------------------------------
# ADF
# ---------------------------------------------------------------------------


def _random_walk(rng, n, drift=0.0, scale=1.0):
    return np.cumsum(rng.normal(loc=drift, scale=scale, size=n))


def test_adf_stationary_series_is_stationary():
    # AR(1) 系数 0.3 的平稳序列 → 强烈拒绝单位根
    rng = np.random.default_rng(7)
    n = 400
    e = rng.normal(size=n)
    y = np.empty(n)
    y[0] = 0.0
    for t in range(1, n):
        y[t] = 0.3 * y[t - 1] + e[t]
    res = qs.adf_test(y)
    assert res["status"] == "ok"
    assert res["is_stationary"] is True
    assert res["p_value"] < 0.05
    assert res["adf_statistic"] < 0.0
    assert res["observations"] >= 10
    assert set(res["critical_values"]) == {"1%", "5%", "10%"}


def test_adf_white_noise_is_stationary():
    rng = np.random.default_rng(11)
    res = qs.adf_test(rng.normal(size=300))
    assert res["status"] == "ok"
    assert res["is_stationary"] is True
    assert res["p_value"] < 0.01


def test_adf_random_walk_is_not_stationary():
    # 纯随机游走 → 无法拒绝单位根
    rng = np.random.default_rng(3)
    y = _random_walk(rng, 500)
    res = qs.adf_test(y)
    assert res["status"] == "ok"
    assert res["is_stationary"] is False
    assert res["p_value"] > 0.05


def test_adf_explicit_lags_and_trend_options():
    rng = np.random.default_rng(5)
    y = _random_walk(rng, 300)
    for trend in ("n", "c", "ct", "ctt"):
        res = qs.adf_test(y, lags=2, trend=trend)
        assert res["status"] == "ok", trend
        assert res["lags_used"] == 2
        assert res["trend"] == trend


def test_adf_more_negative_tau_means_smaller_pvalue():
    # 平稳序列的 tau 应远比随机游走更负, p 更小
    rng = np.random.default_rng(9)
    stat_p = qs.adf_test(rng.normal(size=300))
    rw_p = qs.adf_test(_random_walk(rng, 300))
    assert stat_p["adf_statistic"] < rw_p["adf_statistic"]
    assert stat_p["p_value"] < rw_p["p_value"]


def test_adf_insufficient_and_nonfinite_input():
    assert qs.adf_test([])["status"] == "insufficient_data"
    assert qs.adf_test([1.0])["status"] == "insufficient_data"
    assert qs.adf_test([1.0, 2.0])["status"] == "insufficient_data"
    # 非有限值应被 dropna 剔除; 剩余足够 → ok
    rng = np.random.default_rng(2)
    series = [*rng.normal(size=200), float("nan"), float("inf"), float("-inf")]
    res = qs.adf_test(series)
    assert res["status"] == "ok"
    assert res["is_stationary"] is True


# ---------------------------------------------------------------------------
# Cointegration
# ---------------------------------------------------------------------------


def test_cointegration_constructed_pair_is_cointegrated():
    # y = 1.5·x + 小噪声, x 随机游走 → 残差平稳 → 协整
    rng = np.random.default_rng(21)
    x = _random_walk(rng, 400)
    y = 1.5 * x + rng.normal(scale=0.3, size=400)
    res = qs.cointegration_test(x, y)
    assert res["status"] == "ok"
    assert res["is_cointegrated"] is True
    assert math.isclose(res["hedge_ratio"], 1.5, abs_tol=0.1)
    assert res["p_value"] < 0.05
    assert res["spread"] is not None
    assert res["spread"].size == 400
    # 残差应近似零均值
    assert abs(res["spread_mean"]) < 1.0


def test_cointegration_independent_random_walks_not_cointegrated():
    rng = np.random.default_rng(22)
    x = _random_walk(rng, 500)
    y = _random_walk(rng, 500)  # 两条独立随机游走
    res = qs.cointegration_test(x, y)
    assert res["status"] == "ok"
    assert res["is_cointegrated"] is False
    assert res["p_value"] > 0.05


def test_cointegration_insufficient():
    assert qs.cointegration_test([], [])["status"] == "insufficient_data"
    assert qs.cointegration_test([1.0, 2.0], [3.0, 4.0])["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# GARCH
# ---------------------------------------------------------------------------


def _garch_like_returns(rng, n, omega=0.05, alpha=0.10, beta=0.85):
    # 用 GARCH(1,1) 递推生成带波动聚类的收益序列
    sig2 = np.empty(n)
    eps = rng.normal(size=n)
    sig2[0] = omega / (1 - alpha - beta)
    for t in range(1, n):
        sig2[t] = omega + alpha * (eps[t - 1] ** 2) + beta * sig2[t - 1]
    return eps * np.sqrt(sig2)


def test_garch_nonnegative_variance_moment_estimate():
    rng = np.random.default_rng(31)
    r = _garch_like_returns(rng, 500)
    res = qs.garch_volatility(r)
    assert res["status"] == "ok"
    assert res["params_source"] == "moment"
    var = res["conditional_variance"]
    assert var is not None and var.size == 500
    assert np.all(var >= 0.0)  # 非负方差
    assert res["current_volatility"] is not None
    assert res["current_volatility"] >= 0.0
    # 持续度在合理区间
    assert 0.5 <= res["persistence"] < 1.0
    # 参数非负
    assert res["omega"] >= 0.0 and res["alpha"] >= 0.0 and res["beta"] >= 0.0


def test_garch_provided_params_used_and_stationary():
    rng = np.random.default_rng(32)
    r = rng.normal(scale=1.0, size=300)
    res = qs.garch_volatility(r, omega=0.05, alpha=0.10, beta=0.85)
    assert res["status"] == "ok"
    assert res["params_source"] == "provided"
    assert math.isclose(res["omega"], 0.05)
    assert math.isclose(res["alpha"], 0.10)
    assert math.isclose(res["beta"], 0.85)
    assert math.isclose(res["persistence"], 0.95)
    assert np.all(res["conditional_variance"] >= 0.0)
    # 长期波动率 = sqrt(ω/(1-λ)) = sqrt(0.05/0.05) = 1.0
    assert math.isclose(res["long_run_volatility"], 1.0, rel_tol=1e-9)


def test_garch_invalid_provided_params_falls_back_to_moments():
    rng = np.random.default_rng(33)
    r = rng.normal(size=300)
    # 负参数非法 → 退化到矩估计, 不抛异常
    res = qs.garch_volatility(r, omega=-1.0, alpha=0.1, beta=0.8)
    assert res["status"] == "ok"
    assert res["params_source"] == "moment"
    assert np.all(res["conditional_variance"] >= 0.0)


def test_garch_constant_returns_zero_variance():
    # 常数收益 → 方差 ~ 0, 非负
    res = qs.garch_volatility(np.full(50, 0.01))
    assert res["status"] == "ok"
    assert np.all(res["conditional_variance"] >= 0.0)
    assert res["current_volatility"] is not None


def test_garch_insufficient():
    assert qs.garch_volatility([])["status"] == "insufficient_data"
    assert qs.garch_volatility([0.01])["status"] == "insufficient_data"
    assert qs.garch_volatility([0.01, 0.02])["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# Granger
# ---------------------------------------------------------------------------


def test_granger_lagged_relationship_detected():
    # y_t 依赖 x_{t-1} → x Granger-cause y, lag1 显著
    rng = np.random.default_rng(41)
    n = 400
    x = rng.normal(size=n)
    y = np.empty(n)
    y[0] = 0.0
    for t in range(1, n):
        y[t] = 0.6 * x[t - 1] + 0.3 * y[t - 1] + rng.normal(scale=0.1)
    res = qs.granger_causality(x, y, max_lag=4)
    assert res["status"] == "ok"
    assert res["by_lag"][1]["p_value"] < 0.01
    assert res["by_lag"][1]["is_significant"] is True
    assert res["any_significant"] is True
    assert res["best_lag"] is not None
    assert res["direction"] == "x->y"


def test_granger_independent_series_not_significant():
    rng = np.random.default_rng(42)
    x = rng.normal(size=500)
    y = rng.normal(size=500)
    res = qs.granger_causality(x, y, max_lag=4)
    assert res["status"] == "ok"
    assert res["any_significant"] is False
    assert res["best_p_value"] > 0.05


def test_granger_insufficient():
    assert qs.granger_causality([], [])["status"] == "insufficient_data"
    assert qs.granger_causality([1.0, 2.0], [3.0, 4.0])["status"] == "insufficient_data"


def test_granger_f_distribution_pvalue_sanity():
    # 大样本 + 强关系 → F 很大 → p 很小; 无关系 → F 接近 1
    rng = np.random.default_rng(43)
    n = 600
    x = rng.normal(size=n)
    y = np.empty(n)
    for t in range(1, n):
        y[t] = 0.8 * x[t - 1] + rng.normal(scale=0.2)
    strong = qs.granger_causality(x, y, max_lag=1)
    none = qs.granger_causality(rng.normal(size=n), rng.normal(size=n), max_lag=1)
    assert strong["by_lag"][1]["f_statistic"] > none["by_lag"][1]["f_statistic"]
    assert strong["by_lag"][1]["p_value"] < none["by_lag"][1]["p_value"]


# ---------------------------------------------------------------------------
# VIF
# ---------------------------------------------------------------------------


def test_vif_independent_columns_low_vif():
    rng = np.random.default_rng(51)
    feats = np.column_stack([rng.normal(size=200), rng.normal(size=200), rng.normal(size=200)])
    res = qs.vif_matrix(feats)
    assert res["status"] == "ok"
    for j in range(3):
        assert res["vif"][j] is not None
        assert res["vif"][j] < 2.0
    assert res["max_vif"] < 2.0


def test_vif_collinear_column_fail_soft():
    rng = np.random.default_rng(52)
    c1 = rng.normal(size=200)
    c2 = 2.0 * c1 + 1.0  # 完美共线
    res = qs.vif_matrix(np.column_stack([c1, c2]))
    assert res["status"] == "ok"
    # 完美共线 → R²=1 → None (fail-soft)
    assert res["vif"][0] is None or res["vif"][1] is None


def test_vif_near_collinear_high_vif():
    rng = np.random.default_rng(53)
    c1 = rng.normal(size=300)
    c2 = 2.0 * c1 + rng.normal(scale=0.05, size=300)  # 近似共线
    res = qs.vif_matrix(np.column_stack([c1, c2]))
    assert res["status"] == "ok"
    assert res["vif"][0] is not None and res["vif"][1] is not None
    assert res["max_vif"] > 100.0


def test_vif_single_column_is_one():
    res = qs.vif_matrix(np.array([[1.0], [2.0], [3.0], [4.0]]))
    assert res["status"] == "ok"
    assert math.isclose(res["vif"][0], 1.0)


def test_vif_constant_column_fail_soft():
    # 常数列 tss=0 → None
    res = qs.vif_matrix(np.column_stack([np.full(50, 3.0), np.arange(50.0)]))
    assert res["status"] == "ok"
    assert res["vif"][0] is None


def test_vif_insufficient():
    assert qs.vif_matrix([])["status"] == "insufficient_data"
    assert qs.vif_matrix(np.empty((0, 3)))["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# Unified suite
# ---------------------------------------------------------------------------


def test_suite_dispatch_all_methods():
    rng = np.random.default_rng(61)
    y = rng.normal(size=200)
    assert qs.quant_stats_suite(method="adf", series=y)["test"] == "adf"
    assert qs.quant_stats_suite(method="coint", x=y, y=y)["test"] == "cointegration"
    assert qs.quant_stats_suite(method="garch", returns=y)["test"] == "garch"
    assert qs.quant_stats_suite(method="granger", x=y, y=y)["test"] == "granger"
    feats = np.column_stack([y, y * 2])
    assert qs.quant_stats_suite(method="vif", features=feats)["test"] == "vif"


def test_suite_unknown_method_raises():
    with pytest.raises(ValueError):
        qs.quant_stats_suite(method="nope")


def test_suite_insufficient_inputs():
    assert qs.quant_stats_suite(method="adf", series=[])["status"] == "insufficient_data"
    assert qs.quant_stats_suite(method="garch", returns=[0.01])["status"] == "insufficient_data"
    assert qs.quant_stats_suite(method="vif", features=[])["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# Pure-numpy / no-trading-advice invariants
# ---------------------------------------------------------------------------


def test_module_imports_only_numpy_and_stdlib():
    # 通过实际检查 import 语句 (而非 docstring 文本) 确认未引入禁止依赖
    import inspect

    src = inspect.getsource(qs)
    forbidden = ("statsmodels", "arch", "scipy")
    for line in src.splitlines():
        s = line.strip()
        if not s.startswith(("import ", "from ")):
            continue
        for f in forbidden:
            assert f"import {f}" not in s and f"from {f}" not in s, f"forbidden import: {s}"
    # 模块命名空间同样不应混入这些包
    for name in forbidden:
        assert name not in vars(qs), f"{name} leaked into module namespace"
