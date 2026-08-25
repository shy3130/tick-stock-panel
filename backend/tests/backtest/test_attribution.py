"""Performance attribution 单元测试。

覆盖: 正常归因、权重归一化、空/样本不足、单因子精确关系、常数因子不崩。
所有断言基于 Brinson-Fachler / OLS 解析恒等式, 无随机量。
"""
import numpy as np

from app.backtest import attribution as attr

# ---------------------------------------------------------------------------
# Brinson
# ---------------------------------------------------------------------------


def test_brinson_normal_attribution_sums_to_excess():
    # 两组合计 100% 权重, 基准亦 100%
    wp = [0.3, 0.7]
    rp = [0.10, 0.04]
    wb = [0.5, 0.5]
    rb = [0.06, 0.02]

    res = attr.brinson_attribution(wp, rp, wb, rb)

    assert res["status"] == "ok"
    assert res["normalized"] is False
    # 组合/基准总收益
    assert np.isclose(res["portfolio_return"], 0.3 * 0.10 + 0.7 * 0.04)
    assert np.isclose(res["benchmark_return"], 0.5 * 0.06 + 0.5 * 0.02)
    # 恒等式: allocation+selection+interaction == 超额收益
    total = res["allocation"] + res["selection"] + res["interaction"]
    assert np.isclose(total, res["excess_return"])
    assert np.isclose(res["total_effect"], res["excess_return"])
    assert len(res["groups"]) == 2
    g0 = res["groups"][0]
    assert np.isclose(g0["total_effect"], g0["allocation"] + g0["selection"] + g0["interaction"])


def test_brinson_matches_skill_example_numbers():
    # SKILL.md 表格示例 (允许其四舍五入残差; 这里校验各效应方向与超额收益闭合)
    # Food&Bev / Electronics / Banks / Others
    wp = [0.20, 0.15, 0.05, 0.60]
    rp = [0.15, 0.05, 0.03, 0.08]
    wb = [0.10, 0.12, 0.20, 0.58]
    rb = [0.08, 0.10, 0.02, 0.07]

    res = attr.brinson_attribution(wp, rp, wb, rb)
    assert res["status"] == "ok"
    exp_p = sum(w * r for w, r in zip(wp, rp, strict=True))
    exp_b = sum(w * r for w, r in zip(wb, rb, strict=True))
    assert np.isclose(res["portfolio_return"], exp_p)
    assert np.isclose(res["benchmark_return"], exp_b)
    # 恒等式严格成立 (归一化后无残差)
    assert np.isclose(res["total_effect"], res["excess_return"])
    rb_total = res["benchmark_return"]
    for row in res["groups"]:
        i = ["group_0", "group_1", "group_2", "group_3"].index(row["group"])
        assert np.isclose(row["allocation"], (wp[i] - wb[i]) * (rb[i] - rb_total))
        assert np.isclose(row["selection"], wb[i] * (rp[i] - rb[i]))
        assert np.isclose(row["interaction"], (wp[i] - wb[i]) * (rp[i] - rb[i]))


def test_brinson_normalizes_when_weights_do_not_sum_to_one():
    # 权重总和 2.0 / 基准 1.0 -> 应触发归一化
    wp = [0.6, 1.4]
    rp = [0.10, 0.04]
    wb = [0.5, 0.5]
    rb = [0.06, 0.02]

    res = attr.brinson_attribution(wp, rp, wb, rb)
    assert res["status"] == "ok"
    assert res["normalized"] is True
    # 归一化后与等价的归一化输入结果一致
    eq = attr.brinson_attribution([0.3, 0.7], rp, wb, rb)
    assert np.isclose(res["excess_return"], eq["excess_return"])
    assert np.isclose(res["total_effect"], eq["total_effect"])
    # 归一化使恒等式成立
    assert np.isclose(res["total_effect"], res["excess_return"])


def test_brinson_groups_aggregate_within_industry():
    # 4 个资产分属 2 个行业, 校验组内聚合后与直接给组级输入一致
    wp = [0.2, 0.1, 0.3, 0.4]
    rp = [0.12, 0.06, 0.05, 0.03]
    wb = [0.25, 0.05, 0.4, 0.3]
    rb = [0.08, 0.04, 0.06, 0.02]
    groups = ["A", "A", "B", "B"]

    res = attr.brinson_attribution(wp, rp, wb, rb, groups=groups)
    # 组 A: wp=0.3, r_p=(0.2*0.12+0.1*0.06)/0.3=0.10; wb=0.3, r_b=(0.25*0.08+0.05*0.04)/0.3=0.0733..
    a_row = next(r for r in res["groups"] if r["group"] == "A")
    assert np.isclose(a_row["portfolio_weight"], 0.3)
    assert np.isclose(a_row["benchmark_weight"], 0.3)
    assert np.isclose(a_row["portfolio_return"], (0.2 * 0.12 + 0.1 * 0.06) / 0.3)
    assert np.isclose(a_row["benchmark_return"], (0.25 * 0.08 + 0.05 * 0.04) / 0.3)
    assert len(res["groups"]) == 2
    assert np.isclose(res["total_effect"], res["excess_return"])


def test_brinson_empty_returns_insufficient():
    res = attr.brinson_attribution([], [], [], [])
    assert res["status"] == "insufficient_data"
    assert res["groups"] == []
    assert res["allocation"] is None


# ---------------------------------------------------------------------------
# Fama-French
# ---------------------------------------------------------------------------


def test_fama_french_single_factor_exact_relationship():
    # y = 2 + 3*x (含截距, 完美线性) -> alpha=2, beta=3, r_squared≈1, 残差≈0
    rng = np.random.default_rng(0)
    x = rng.normal(0, 1, 80)
    y = 2.0 + 3.0 * x

    res = attr.fama_french_attribution(y, x)
    assert res["status"] == "ok"
    assert np.isclose(res["alpha"], 2.0)
    assert np.isclose(res["betas"]["factor_1"], 3.0)
    assert np.isclose(res["r_squared"], 1.0, atol=1e-10)
    assert np.isclose(res["residual_volatility"], 0.0)
    assert res["observations"] == 80


def test_fama_french_multi_factor_with_risk_free_and_dict_factors():
    rng = np.random.default_rng(1)
    n = 120
    mkt = rng.normal(0.0008, 0.01, n)
    smb = rng.normal(0.0003, 0.008, n)
    rf = np.full(n, 0.0001)
    # 真实关系 + 噪声
    y = 0.001 + 1.1 * mkt + 0.4 * smb + rng.normal(0, 0.001, n)

    res = attr.fama_french_attribution(y, {"MKT": mkt, "SMB": smb}, risk_free=rf)
    assert res["status"] == "ok"
    assert res["observations"] == n
    assert set(res["betas"]) == {"MKT", "SMB"}
    assert abs(res["betas"]["MKT"] - 1.1) < 0.1
    assert abs(res["betas"]["SMB"] - 0.4) < 0.1
    assert 0.0 <= res["r_squared"] <= 1.0
    # 贡献 = beta * mean(factor)
    assert np.isclose(res["contributions"]["MKT"], res["betas"]["MKT"] * mkt.mean())
    # 均值超额收益恒等: mean(y-rf) ≈ alpha + Σ contributions
    mean_excess = float(np.mean(y - rf))
    recon = res["alpha"] + sum(res["contributions"].values())
    assert np.isclose(recon, mean_excess, atol=1e-9)


def test_fama_french_scalar_risk_free_broadcasts():
    rng = np.random.default_rng(2)
    x = rng.normal(0, 1, 60)
    y = 0.5 + 1.5 * x
    res = attr.fama_french_attribution(y, x, risk_free=0.0)
    assert res["status"] == "ok"
    assert np.isclose(res["alpha"], 0.5)
    assert np.isclose(res["betas"]["factor_1"], 1.5)


def test_fama_french_constant_factor_does_not_crash():
    # 常数因子与截距共线 -> 设计矩阵秩不足 -> 返回 insufficient_data (不抛异常)
    const = np.full(50, 0.05)
    y = np.linspace(-0.01, 0.02, 50)
    res = attr.fama_french_attribution(y, const)
    assert res["status"] == "insufficient_data"
    assert res["alpha"] is None


def test_fama_french_insufficient_sample():
    res = attr.fama_french_attribution([0.01], [0.02])
    assert res["status"] == "insufficient_data"
    assert res["observations"] == 1

    res_empty = attr.fama_french_attribution([], [])
    assert res_empty["status"] == "insufficient_data"


def test_fama_french_too_few_observers_for_two_factors():
    # 2 因子需秩 3, 仅 2 个样本 -> 秩不足
    res = attr.fama_french_attribution([0.01, 0.02], {"A": [0.1, 0.2], "B": [0.3, 0.4]})
    assert res["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# Unified dispatch
# ---------------------------------------------------------------------------


def test_performance_attribution_dispatch_brinson():
    res = attr.performance_attribution(
        method="brinson",
        portfolio_weights=[0.5, 0.5],
        portfolio_returns=[0.1, 0.0],
        benchmark_weights=[0.5, 0.5],
        benchmark_returns=[0.05, 0.05],
    )
    assert res["status"] == "ok"
    assert np.isclose(res["excess_return"], 0.0)


def test_performance_attribution_dispatch_fama_french():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    y = np.array([3.0, 5.0, 7.0, 9.0, 11.0])  # y = 1 + 2x
    res = attr.performance_attribution(
        method="fama_french", portfolio_returns=y, factor_returns=x
    )
    assert res["status"] == "ok"
    assert np.isclose(res["alpha"], 1.0)
    assert np.isclose(res["betas"]["factor_1"], 2.0)


def test_performance_attribution_unknown_method_raises():
    import pytest

    with pytest.raises(ValueError):
        attr.performance_attribution(method="nope")
