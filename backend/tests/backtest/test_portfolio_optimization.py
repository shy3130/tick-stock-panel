"""组合优化器 (portfolio_optimization) 测试。

覆盖：四种方法基本正确性、空/样本不足、恒定收益、奇异协方差、权重边界、
risk parity 收敛、确定性、Sharpe 优于等权等。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.backtest.portfolio_optimization import (
    OptimizationResult,
    VALID_METHODS,
    optimize_portfolio,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_returns() -> np.ndarray:
    """3 资产、500 期的非退化收益矩阵（不同波动率 + 相关性）."""
    rng = np.random.default_rng(42)
    base = rng.normal(size=(500, 3))
    scales = np.array([0.005, 0.02, 0.015])
    corr = np.array([
        [1.0, 0.3, -0.1],
        [0.3, 1.0, 0.2],
        [-0.1, 0.2, 1.0],
    ])
    L = np.linalg.cholesky(corr)
    return base @ L.T * scales  # shape (500, 3)


@pytest.fixture
def drift_returns(sample_returns: np.ndarray) -> np.ndarray:
    """在 sample_returns 基础上加入强差异化漂移，使期望收益排序在 500 期下稳定。

    T=500 时均值标准误约 scale/√500；取漂移远大于 SE 以保证：
    asset 0 > 0, asset 1 > 0 (最高), asset 2 < 0。
    """
    drifts = np.array([0.0005, 0.0030, -0.0015])
    return sample_returns + drifts


# ---------------------------------------------------------------------------
# 基本属性：所有方法
# ---------------------------------------------------------------------------

class TestAllMethodsBasicProperties:
    """所有合法方法在正常输入上的共同不变量。"""

    @pytest.mark.parametrize("method", VALID_METHODS)
    def test_returns_valid_result_type(self, sample_returns, method):
        res = optimize_portfolio(sample_returns, method)
        assert isinstance(res, OptimizationResult)
        assert res.method == method

    @pytest.mark.parametrize("method", VALID_METHODS)
    def test_weights_sum_to_one_long_only(self, sample_returns, method):
        res = optimize_portfolio(sample_returns, method)
        assert res.weights.size == 3
        assert np.isclose(res.weights.sum(), 1.0, atol=1e-10)
        assert np.all(res.weights >= -1e-12)

    @pytest.mark.parametrize("method", VALID_METHODS)
    def test_diagnostics_finite(self, sample_returns, method):
        res = optimize_portfolio(sample_returns, method)
        assert np.isfinite(res.expected_return)
        assert np.isfinite(res.volatility)
        assert np.isfinite(res.sharpe)
        assert res.volatility >= 0

    @pytest.mark.parametrize("method", VALID_METHODS)
    def test_volatility_matches_definition(self, sample_returns, method):
        res = optimize_portfolio(sample_returns, method)
        r = sample_returns[np.isfinite(sample_returns).all(axis=1)]
        cov = np.cov(r, rowvar=False)
        expected_vol = np.sqrt(res.weights @ cov @ res.weights)
        assert np.isclose(res.volatility, expected_vol, atol=1e-10)

    @pytest.mark.parametrize("method", VALID_METHODS)
    def test_sharpe_matches_definition(self, sample_returns, method):
        rf = 0.0001
        res = optimize_portfolio(sample_returns, method, risk_free_rate=rf)
        if res.volatility > 1e-15:
            expected = (res.expected_return - rf) / res.volatility
            assert np.isclose(res.sharpe, expected, atol=1e-10)


# ---------------------------------------------------------------------------
# equal_weight
# ---------------------------------------------------------------------------

class TestEqualWeight:
    def test_uniform_weights(self, sample_returns):
        res = optimize_portfolio(sample_returns, "equal_weight")
        assert np.allclose(res.weights, [1/3, 1/3, 1/3])
        assert res.converged is True
        assert res.iterations == 0
        assert res.status == "ok"

    def test_no_iteration_needed(self):
        r = np.random.default_rng(0).normal(size=(50, 5))
        res = optimize_portfolio(r, "equal_weight")
        assert res.iterations == 0

    def test_infeasible_max_weight_warning(self):
        r = np.random.default_rng(0).normal(size=(50, 4))
        res = optimize_portfolio(r, "equal_weight", max_weight=0.2)
        # 1/4 = 0.25 > 0.2 → 不可行
        assert res.status == "degraded"
        assert any("max_weight" in w for w in res.warnings)

    def test_feasible_bounds_no_warning(self):
        r = np.random.default_rng(0).normal(size=(50, 4))
        res = optimize_portfolio(r, "equal_weight", min_weight=0.1, max_weight=0.5)
        # 1/4 = 0.25 ∈ [0.1, 0.5]
        assert res.status == "ok"


# ---------------------------------------------------------------------------
# minimum_variance
# ---------------------------------------------------------------------------

class TestMinimumVariance:
    def test_lower_variance_than_equal_weight(self, sample_returns):
        mv = optimize_portfolio(sample_returns, "minimum_variance")
        eq = optimize_portfolio(sample_returns, "equal_weight")
        assert mv.volatility <= eq.volatility + 1e-12

    def test_converged(self, sample_returns):
        res = optimize_portfolio(sample_returns, "minimum_variance")
        assert res.converged is True
        assert res.iterations > 0

    def test_respects_weight_bounds(self, sample_returns):
        res = optimize_portfolio(sample_returns, "minimum_variance",
                                 min_weight=0.1, max_weight=0.6)
        assert np.all(res.weights >= 0.1 - 1e-8)
        assert np.all(res.weights <= 0.6 + 1e-8)
        assert np.isclose(res.weights.sum(), 1.0, atol=1e-8)

    def test_two_asset_minimum_variance(self):
        """2 资产最小方差有解析解，验证数值解接近."""
        rng = np.random.default_rng(99)
        r = rng.normal(size=(500, 2)) * np.array([0.01, 0.03])
        res = optimize_portfolio(r, "minimum_variance")
        cov = np.cov(r, rowvar=False)
        # 解析: w0 = (cov[1,1] - cov[0,1]) / (cov[0,0] + cov[1,1] - 2*cov[0,1])
        denom = cov[0, 0] + cov[1, 1] - 2 * cov[0, 1]
        if denom > 0:
            w0_analytic = (cov[1, 1] - cov[0, 1]) / denom
            assert np.isclose(res.weights[0], w0_analytic, atol=0.02)


# ---------------------------------------------------------------------------
# maximum_sharpe
# ---------------------------------------------------------------------------

class TestMaximumSharpe:
    def test_higher_sharpe_than_equal_weight(self, drift_returns):
        ms = optimize_portfolio(drift_returns, "maximum_sharpe")
        eq = optimize_portfolio(drift_returns, "equal_weight")
        # 最大 Sharpe 至少不差于等权
        assert ms.sharpe >= eq.sharpe - 1e-6

    def test_allocates_to_higher_return_asset(self, drift_returns):
        """资产 1 有最高漂移，应获得较大权重."""
        res = optimize_portfolio(drift_returns, "maximum_sharpe")
        assert res.weights[1] > res.weights[2]  # 漂移 0.001 > -0.0003

    def test_all_negative_excess_falls_back(self):
        """全部超额收益 ≤ 0 → 退化为最小方差."""
        rng = np.random.default_rng(5)
        r = rng.normal(size=(100, 3), loc=-0.001, scale=0.01)
        res = optimize_portfolio(r, "maximum_sharpe", risk_free_rate=0.0)
        assert res.status == "degraded"
        assert any("超额收益" in w or "退化" in w for w in res.warnings)

    def test_with_risk_free_rate(self, drift_returns):
        rf = 0.0003
        res = optimize_portfolio(drift_returns, "maximum_sharpe",
                                 risk_free_rate=rf)
        assert np.isfinite(res.sharpe)
        assert np.all(res.weights >= -1e-10)

    def test_respects_weight_bounds(self, drift_returns):
        res = optimize_portfolio(drift_returns, "maximum_sharpe",
                                 min_weight=0.15, max_weight=0.5)
        assert np.all(res.weights >= 0.15 - 1e-8)
        assert np.all(res.weights <= 0.5 + 1e-8)
        assert np.isclose(res.weights.sum(), 1.0, atol=1e-8)


# ---------------------------------------------------------------------------
# risk_parity
# ---------------------------------------------------------------------------

class TestRiskParity:
    def test_equal_risk_contribution(self, sample_returns):
        res = optimize_portfolio(sample_returns, "risk_parity")
        assert res.converged is True
        r = sample_returns[np.isfinite(sample_returns).all(axis=1)]
        cov = np.cov(r, rowvar=False) + np.eye(3) * 1e-8
        rc = res.weights * (cov @ res.weights)
        rc_norm = rc / rc.sum()
        # 每个资产的归一化风险贡献应接近 1/3
        assert np.allclose(rc_norm, [1/3, 1/3, 1/3], atol=0.01)

    def test_converged_with_reasonable_iterations(self, sample_returns):
        res = optimize_portfolio(sample_returns, "risk_parity", max_iter=1000)
        assert res.converged is True
        assert res.iterations <= 1000
        assert res.iterations > 0

    def test_low_volatility_asset_gets_more_weight(self, sample_returns):
        """波动率最低的资产 0 应获得比波动率最高的资产 1 更大权重."""
        res = optimize_portfolio(sample_returns, "risk_parity")
        assert res.weights[0] > res.weights[1]

    def test_tolerance_affects_iterations(self, sample_returns):
        """更宽松的容差应使用更少（或同等）迭代."""
        loose = optimize_portfolio(sample_returns, "risk_parity",
                                   tol=1e-3, max_iter=1000)
        tight = optimize_portfolio(sample_returns, "risk_parity",
                                   tol=1e-12, max_iter=1000)
        # 宽容差收敛更快（或相等）
        assert loose.iterations <= tight.iterations + 5

    def test_deterministic(self, sample_returns):
        """相同输入两次调用结果完全一致."""
        r1 = optimize_portfolio(sample_returns, "risk_parity")
        r2 = optimize_portfolio(sample_returns, "risk_parity")
        np.testing.assert_array_equal(r1.weights, r2.weights)
        assert r1.iterations == r2.iterations


# ---------------------------------------------------------------------------
# 边界情况：空输入 / 样本不足
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_array(self):
        res = optimize_portfolio(np.array([]), "equal_weight")
        assert res.status == "invalid_input"
        assert res.weights.size == 0

    def test_1d_array_rejected(self):
        res = optimize_portfolio(np.array([0.01, 0.02, 0.03]), "equal_weight")
        assert res.status == "invalid_input"

    def test_single_period_insufficient(self):
        r = np.array([[0.01, 0.02, 0.03]])
        res = optimize_portfolio(r, "minimum_variance")
        assert res.status == "insufficient_data"
        assert np.allclose(res.weights, [1/3, 1/3, 1/3])
        assert res.converged is False

    def test_single_asset(self):
        r = np.array([[0.01], [0.02], [0.03]])
        res = optimize_portfolio(r, "minimum_variance")
        assert res.weights.size == 1
        assert np.isclose(res.weights[0], 1.0)
        assert res.converged is True

    def test_nan_rows_filtered(self):
        r = np.array([
            [np.nan, 0.02, 0.03],
            [0.01, 0.02, 0.03],
            [0.04, 0.05, 0.06],
            [np.inf, 0.02, 0.03],
            [0.07, 0.08, 0.09],
        ])
        res = optimize_portfolio(r, "minimum_variance")
        assert res.status in ("ok", "degraded")
        assert np.isclose(res.weights.sum(), 1.0, atol=1e-10)


# ---------------------------------------------------------------------------
# 恒定收益
# ---------------------------------------------------------------------------

class TestConstantReturns:
    def test_zero_variance_degrades_gracefully(self):
        r = np.full((50, 3), 0.001)
        res = optimize_portfolio(r, "minimum_variance")
        assert res.status == "degraded"
        assert res.volatility < 1e-6
        assert np.allclose(res.weights, [1/3, 1/3, 1/3], atol=0.01)

    def test_risk_parity_constant_returns(self):
        r = np.full((50, 3), 0.001)
        res = optimize_portfolio(r, "risk_parity")
        assert res.status == "degraded"
        assert np.allclose(res.weights, [1/3, 1/3, 1/3], atol=0.05)

    def test_equal_weight_constant_returns_ok(self):
        r = np.full((50, 3), 0.001)
        res = optimize_portfolio(r, "equal_weight")
        assert res.status == "ok"
        assert np.isclose(res.volatility, 0.0, atol=1e-10)
        assert res.sharpe == 0.0  # vol ≈ 0 → sharpe = 0


# ---------------------------------------------------------------------------
# 奇异协方差
# ---------------------------------------------------------------------------

class TestSingularCovariance:
    def test_perfectly_correlated_assets(self):
        """完全相关的两资产（协方差奇异）."""
        base = np.random.default_rng(7).normal(size=(100, 1)) * 0.01
        r = np.hstack([base, base * 2])  # 完美相关
        res = optimize_portfolio(r, "minimum_variance")
        assert res.status in ("ok", "degraded")
        assert np.isclose(res.weights.sum(), 1.0, atol=1e-8)
        assert np.all(res.weights >= -1e-10)

    def test_duplicate_columns(self):
        rng = np.random.default_rng(3)
        col = rng.normal(size=(100, 1)) * 0.02
        r = np.hstack([col, col, col])  # 三列完全相同 → 严重奇异
        res = optimize_portfolio(r, "risk_parity")
        assert res.status in ("ok", "degraded")
        assert np.isclose(res.weights.sum(), 1.0, atol=1e-8)
        # 完全相同的资产应获得近似等权
        assert np.allclose(res.weights, [1/3, 1/3, 1/3], atol=0.05)

    def test_max_sharpe_singular_cov(self):
        rng = np.random.default_rng(3)
        col = rng.normal(size=(100, 1)) * 0.02
        r = np.hstack([col, col, col])
        res = optimize_portfolio(r, "maximum_sharpe")
        assert res.status in ("ok", "degraded")
        assert np.isclose(res.weights.sum(), 1.0, atol=1e-8)


# ---------------------------------------------------------------------------
# 权重边界约束
# ---------------------------------------------------------------------------

class TestWeightBounds:
    def test_infeasible_min_weight(self, sample_returns):
        """min_weight=0.5, N=3 → 1.5 > 1 不可行."""
        res = optimize_portfolio(sample_returns, "minimum_variance",
                                 min_weight=0.5)
        assert res.status == "degraded"
        assert np.allclose(res.weights, [1/3, 1/3, 1/3])

    def test_infeasible_max_weight(self, sample_returns):
        """max_weight=0.2, N=3 → 0.6 < 1 不可行."""
        res = optimize_portfolio(sample_returns, "risk_parity",
                                 max_weight=0.2)
        assert res.status == "degraded"
        assert np.allclose(res.weights, [1/3, 1/3, 1/3])

    def test_tight_bounds_enforced(self, sample_returns):
        res = optimize_portfolio(sample_returns, "minimum_variance",
                                 min_weight=0.2, max_weight=0.5)
        assert np.all(res.weights >= 0.2 - 1e-8)
        assert np.all(res.weights <= 0.5 + 1e-8)
        assert np.isclose(res.weights.sum(), 1.0, atol=1e-8)

    def test_negative_min_weight_clipped(self, sample_returns):
        res = optimize_portfolio(sample_returns, "minimum_variance",
                                 min_weight=-0.1)
        assert any("min_weight" in w and "截断" in w for w in res.warnings)
        assert np.all(res.weights >= -1e-12)


# ---------------------------------------------------------------------------
# 确定性 & 无效方法
# ---------------------------------------------------------------------------

class TestDeterminismAndValidation:
    @pytest.mark.parametrize("method", VALID_METHODS)
    def test_deterministic_across_calls(self, sample_returns, method):
        r1 = optimize_portfolio(sample_returns, method)
        r2 = optimize_portfolio(sample_returns, method)
        np.testing.assert_array_equal(r1.weights, r2.weights)

    def test_unknown_method(self, sample_returns):
        res = optimize_portfolio(sample_returns, "black_litterman")
        assert res.status == "invalid_input"
        assert res.weights.size == 0
        assert any("未知方法" in w for w in res.warnings)

    def test_default_method_not_in_valid(self, sample_returns):
        """ensure old optimizer method names are NOT accepted here."""
        res = optimize_portfolio(sample_returns, "equal_vol")
        assert res.status == "invalid_input"


# ---------------------------------------------------------------------------
# risk_parity 与其他方法的区分
# ---------------------------------------------------------------------------

class TestMethodDifferentiation:
    def test_risk_pity_vs_min_variance_different_weights(self, sample_returns):
        rp = optimize_portfolio(sample_returns, "risk_parity")
        mv = optimize_portfolio(sample_returns, "minimum_variance")
        # 两种方法在非退化输入上应给出不同权重
        assert not np.allclose(rp.weights, mv.weights, atol=1e-4)

    def test_min_variance_concentrates_more(self, sample_returns):
        """最小方差通常比 risk parity 更集中（权重方差更大）."""
        mv = optimize_portfolio(sample_returns, "minimum_variance")
        rp = optimize_portfolio(sample_returns, "risk_parity")
        mv_concentration = np.var(mv.weights)
        rp_concentration = np.var(rp.weights)
        # 最小方差的集中度 >= risk parity（更分散）
        assert mv_concentration >= rp_concentration - 1e-6
