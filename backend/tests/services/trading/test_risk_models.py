"""``risk_models`` 纯函数风险度量测试。

覆盖: 正常序列 / 样本不足 / 空数据 / 常量序列 / 确定性 seed / 极端 scenario。
不依赖外部数据源,纯计算可单测。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services.trading.risk_models import (
    evt_tail_summary,
    historical_cvar,
    historical_var,
    monte_carlo_var,
    parametric_var,
    portfolio_risk_models,
    stress_test,
)

# 构造一段可复现的"正常"日收益序列 (n=300, 带肥尾)。
_RNG = np.random.default_rng(7)
_NORMAL_RETURNS = np.concatenate(
    [
        _RNG.normal(0.0004, 0.018, 290),
        _RNG.normal(-0.03, 0.04, 10),  # 注入若干大跌, 形成左尾
    ]
)


# --------------------------------------------------------------------------- #
# historical_var
# --------------------------------------------------------------------------- #
class TestHistoricalVar:
    def test_normal_returns_positive_loss(self):
        res = historical_var(_NORMAL_RETURNS, 0.95)
        assert res["status"] == "ok"
        assert res["method"] == "historical"
        assert res["confidence"] == pytest.approx(0.95)
        assert res["observations"] == 300
        # 注入了大跌, 95% VaR 应为正损失
        assert res["var"] is not None
        assert res["var"] > 0

    def test_matches_manual_quantile_formula(self):
        r = np.array(sorted(_NORMAL_RETURNS))
        n = r.size
        idx = min(int((1 - 0.95) * n), n - 1)
        expected = -float(r[idx])
        assert historical_var(_NORMAL_RETURNS, 0.95)["var"] == pytest.approx(expected)

    def test_insufficient_samples(self):
        res = historical_var([0.01, -0.02, 0.005], 0.95)
        assert res["status"] == "insufficient_data"
        assert res["var"] is None
        assert res["observations"] == 3

    def test_empty_data(self):
        res = historical_var([], 0.95)
        assert res["status"] == "insufficient_data"
        assert res["var"] is None
        assert res["observations"] == 0

    def test_constant_series(self):
        res = historical_var(np.full(50, 0.001), 0.95)
        # 全正收益 => 没有损失, var = -0.001 (负的"损失")
        assert res["status"] == "ok"
        assert res["var"] == pytest.approx(-0.001)

    def test_drops_non_finite(self):
        r = np.concatenate([_NORMAL_RETURNS, [np.nan, np.inf, -np.inf]])
        res = historical_var(r, 0.95)
        assert res["status"] == "ok"
        assert res["observations"] == 300


# --------------------------------------------------------------------------- #
# historical_cvar
# --------------------------------------------------------------------------- #
class TestHistoricalCvar:
    def test_cvar_exceeds_var(self):
        hv = historical_var(_NORMAL_RETURNS, 0.95)
        res = historical_cvar(_NORMAL_RETURNS, 0.95)
        assert res["status"] == "ok"
        assert res["cvar"] is not None
        # CVaR 通常 >= VaR (更保守)
        assert res["cvar"] >= hv["var"] - 1e-9

    def test_matches_manual_formula(self):
        hv = historical_var(_NORMAL_RETURNS, 0.95)
        var_thresh = -float(hv["var"])
        tail = _NORMAL_RETURNS[_NORMAL_RETURNS < var_thresh]
        expected = -float(np.mean(tail)) if tail.size else float(hv["var"])
        res_cvar = historical_cvar(_NORMAL_RETURNS, 0.95)
        assert res_cvar["cvar"] == pytest.approx(expected)

    def test_insufficient_samples(self):
        res = historical_cvar([0.01, 0.02], 0.95)
        assert res["status"] == "insufficient_data"
        assert res["cvar"] is None
        assert res["var"] is None

    def test_empty_data(self):
        assert historical_cvar([], 0.95)["status"] == "insufficient_data"

    def test_constant_series_no_crash(self):
        res = historical_cvar(np.zeros(60), 0.95)
        assert res["status"] == "ok"
        # 全零: 无尾部损失 => cvar 回退为 var = 0
        assert res["cvar"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# parametric_var
# --------------------------------------------------------------------------- #
class TestParametricVar:
    def test_normal_returns(self):
        res = parametric_var(_NORMAL_RETURNS, 0.95)
        assert res["status"] == "ok"
        assert res["method"] == "parametric"
        assert res["var"] is not None and res["var"] > 0
        assert res["mean"] is not None
        assert res["std"] is not None

    def test_matches_manual_formula(self):
        # norm.ppf(0.05) = -1.6448536... (已知标准正态 5% 分位)
        from app.services.trading.risk_models import _norm_ppf

        mu = float(np.mean(_NORMAL_RETURNS))
        sigma = float(np.std(_NORMAL_RETURNS, ddof=1))
        z = _norm_ppf(1 - 0.95)
        expected = -(mu + z * sigma)
        assert parametric_var(_NORMAL_RETURNS, 0.95)["var"] == pytest.approx(expected)

    def test_norm_ppf_accuracy(self):
        from app.services.trading.risk_models import _norm_ppf
        # 对照标准正态分位表 (精度足够风险用途)
        assert _norm_ppf(0.05) == pytest.approx(-1.6448536270, abs=1e-6)
        assert _norm_ppf(0.01) == pytest.approx(-2.3263478740, abs=1e-6)
        assert _norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)

    def test_higher_confidence_larger_var(self):
        v95 = parametric_var(_NORMAL_RETURNS, 0.95)["var"]
        v99 = parametric_var(_NORMAL_RETURNS, 0.99)["var"]
        assert v99 > v95

    def test_constant_series(self):
        res = parametric_var(np.full(40, 0.002), 0.95)
        assert res["status"] == "ok"
        # sigma=0 => var = -mu = -0.002
        assert res["std"] == pytest.approx(0.0)
        assert res["var"] == pytest.approx(-0.002)

    def test_insufficient_and_empty(self):
        assert parametric_var([0.01], 0.95)["status"] == "insufficient_data"
        assert parametric_var([], 0.95)["var"] is None


# --------------------------------------------------------------------------- #
# monte_carlo_var
# --------------------------------------------------------------------------- #
class TestMonteCarloVar:
    def test_deterministic_with_seed(self):
        a = monte_carlo_var(_NORMAL_RETURNS, 0.95, simulations=5000, seed=42)
        b = monte_carlo_var(_NORMAL_RETURNS, 0.95, simulations=5000, seed=42)
        assert a == b
        assert a["seed"] == 42
        assert a["simulations"] == 5000

    def test_different_seed_changes_result(self):
        a = monte_carlo_var(_NORMAL_RETURNS, 0.95, simulations=5000, seed=1)
        b = monte_carlo_var(_NORMAL_RETURNS, 0.95, simulations=5000, seed=2)
        assert a["var"] != b["var"]

    def test_positive_loss_and_stats(self):
        res = monte_carlo_var(_NORMAL_RETURNS, 0.95)
        assert res["status"] == "ok"
        assert res["var"] is not None and res["var"] > 0
        assert res["cvar"] is not None
        assert 0.0 <= res["probLoss"] <= 1.0

    def test_insufficient_samples(self):
        res = monte_carlo_var([0.01, 0.02], 0.95, seed=0)
        assert res["status"] == "insufficient_data"
        assert res["var"] is None
        assert res["seed"] == 0

    def test_empty_data(self):
        assert monte_carlo_var([], 0.95)["status"] == "insufficient_data"

    def test_constant_series(self):
        res = monte_carlo_var(np.full(40, 0.001), 0.95, seed=42)
        assert res["status"] == "ok"
        # sigma=0 => 所有模拟值 = mu => var = -mu
        assert res["var"] == pytest.approx(-0.001)


# --------------------------------------------------------------------------- #
# stress_test
# --------------------------------------------------------------------------- #
class TestStressTest:
    def test_extreme_scenario_breaches_var(self):
        scenarios = {"mild": -0.01, "extreme": -0.65}
        res = stress_test(_NORMAL_RETURNS, scenarios)
        assert res["status"] == "ok"
        assert res["scenarioCount"] == 2
        extreme = res["scenarios"]["extreme"]
        assert extreme["loss"] == pytest.approx(0.65)
        assert extreme["breachesVar95"] is True
        assert res["worstScenario"] == "extreme"
        assert res["worstLoss"] == pytest.approx(0.65)

    def test_does_not_mutate_input(self):
        scenarios = {"a": -0.05, "b": -0.10}
        snapshot = {"a": -0.05, "b": -0.10}
        stress_test(_NORMAL_RETURNS, scenarios)
        assert scenarios == snapshot

    def test_empty_scenarios(self):
        res = stress_test(_NORMAL_RETURNS, {})
        assert res["scenarioCount"] == 0
        assert res["worstScenario"] is None
        assert res["var95"] is not None  # 基准仍可算

    def test_positive_shock_is_gain(self):
        res = stress_test(_NORMAL_RETURNS, {"rally": 0.08})
        assert res["scenarios"]["rally"]["loss"] == pytest.approx(-0.08)
        assert res["scenarios"]["rally"]["breachesVar95"] is False

    def test_insufficient_returns_still_reports_scenarios(self):
        res = stress_test([0.01, 0.02], {"crash": -0.5})
        assert res["status"] == "insufficient_data"
        assert res["var95"] is None
        assert res["scenarios"]["crash"]["loss"] == pytest.approx(0.5)
        assert res["scenarios"]["crash"]["breachesVar95"] is None


# --------------------------------------------------------------------------- #
# evt_tail_summary
# --------------------------------------------------------------------------- #
class TestEvtTailSummary:
    def test_fat_tail_detected(self):
        res = evt_tail_summary(_NORMAL_RETURNS, 0.95)
        assert res["status"] == "ok"
        assert res["nExceedances"] >= 5
        assert res["shapeXi"] is not None
        assert res["scaleSigma"] is not None
        assert res["tailType"] in {
            "fat tail (dangerous)",
            "exponential tail",
            "thin tail (bounded)",
        }
        assert res["skewness"] is not None
        assert res["excessKurtosis"] is not None
        assert res["tailRatio"] is not None

    def test_threshold_quantile_controls_exceedances(self):
        r95 = evt_tail_summary(_NORMAL_RETURNS, 0.95)
        r99 = evt_tail_summary(_NORMAL_RETURNS, 0.99)
        # 更极端的分位 => 更少超限点
        assert r99["nExceedances"] <= r95["nExceedances"]

    def test_insufficient_samples(self):
        res = evt_tail_summary([0.01, 0.02, 0.03], 0.95)
        assert res["status"] == "insufficient_data"
        assert res["shapeXi"] is None

    def test_empty_data(self):
        assert evt_tail_summary([], 0.95)["status"] == "insufficient_data"

    def test_too_few_exceedances(self):
        # 大量几乎常数的数据 => 尾部超限不足
        res = evt_tail_summary(np.full(60, 0.001), 0.95)
        assert res["status"] == "insufficient_data"
        assert res["shapeXi"] is None


# --------------------------------------------------------------------------- #
# portfolio_risk_models
# --------------------------------------------------------------------------- #
class TestPortfolioRiskModels:
    def test_aggregates_all_models(self):
        res = portfolio_risk_models(_NORMAL_RETURNS, 0.95, simulations=2000, seed=42)
        assert res["status"] == "ok"
        assert res["source"].endswith("(ported; risk-perspective only)")
        assert res["historicalVar"] is not None
        assert res["historicalCvar"] is not None
        assert res["parametricVar"] is not None
        assert res["monteCarlo"]["var"] is not None
        assert res["evt"]["status"] == "ok"
        desc = res["descriptive"]
        assert desc["mean"] is not None
        assert desc["annualizedVolatility"] is not None

    def test_deterministic(self):
        a = portfolio_risk_models(_NORMAL_RETURNS, 0.95, simulations=1000, seed=42)
        b = portfolio_risk_models(_NORMAL_RETURNS, 0.95, simulations=1000, seed=42)
        assert a == b

    def test_insufficient_data(self):
        res = portfolio_risk_models([0.01, 0.02], 0.95)
        assert res["status"] == "insufficient_data"
        assert res["historicalVar"] is None
        assert res["monteCarlo"] is None
        assert res["evt"] is None

    def test_empty_data(self):
        res = portfolio_risk_models([], 0.95)
        assert res["status"] == "insufficient_data"
        assert res["observations"] == 0
        assert res["descriptive"]["mean"] is None

    def test_accepts_python_list(self):
        res = portfolio_risk_models(_NORMAL_RETURNS.tolist(), 0.95, simulations=500)
        assert res["status"] == "ok"
