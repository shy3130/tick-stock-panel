"""PSR (概率 Sharpe) 与交易级 bootstrap 净值带单元测试 — A3 可信度增强。

覆盖验收项:
* PSR: 正态合成正 SR → >0.5 且随 n 增大趋近 1; SR=基准 (年化口径传入) → ≈0.5;
  短样本 (<20) / 常量序列 (std 退化) → None。
* band: p05≤p25≤p50≤p75≤p95 逐点成立; 同 seed 确定性; <10 笔 / n_boot<100 → None;
  大 n_boot 下 p50 末值接近原始顺序复利末值 (容忍抽样波动)。

所有断言基于固定 seed 的确定性样本或解析恒等式 (基准相等 → z=0 → Φ(0)=0.5)。
"""

import json
import math

import numpy as np

from app.backtest import metrics as mt
from app.backtest import robustness as rb

_DAILY = mt.MetricContext("daily")


# ---------------------------------------------------------------------------
# probabilistic_sharpe_ratio
# ---------------------------------------------------------------------------

def test_psr_positive_sharpe_above_half_and_approaches_one_with_n():
    # 日频 μ=0.001, σ=0.01 → 年化 SR ≈ 1.587; PSR 应随样本量向 1 收敛
    # (独立样本, 固定 seed: 0.6699 < 0.9734 < 0.99999985)
    p_small = mt.probabilistic_sharpe_ratio(np.random.default_rng(103).normal(0.001, 0.01, 60), _DAILY)
    p_mid = mt.probabilistic_sharpe_ratio(np.random.default_rng(103).normal(0.001, 0.01, 250), _DAILY)
    p_big = mt.probabilistic_sharpe_ratio(np.random.default_rng(103).normal(0.001, 0.01, 1500), _DAILY)
    assert p_small is not None and p_mid is not None and p_big is not None
    assert p_small > 0.5
    assert p_small < p_mid < p_big
    assert p_big > 0.98  # 大样本下趋近 1


def test_psr_at_annualized_benchmark_is_exactly_half():
    # 基准取自身年化 Sharpe (年化输入, 内部换算每期) → SR−SR*=0 → Φ(0)=0.5
    rng = np.random.default_rng(7)
    rets = rng.normal(0.0008, 0.012, 300)
    sr_ann = mt.annualized_sharpe(rets, _DAILY)
    assert sr_ann is not None
    psr = mt.probabilistic_sharpe_ratio(rets, _DAILY, benchmark_sr=sr_ann)
    assert psr is not None
    assert math.isclose(psr, 0.5, abs_tol=1e-12)


def test_psr_benchmark_annualization_convention():
    # benchmark_sr 按年化口径: 传入一个略低于自身年化 SR 的基准 → PSR 应明显高于 0.5
    rng = np.random.default_rng(9)
    rets = rng.normal(0.001, 0.008, 500)
    sr_ann = mt.annualized_sharpe(rets, _DAILY)
    assert sr_ann is not None
    psr_below = mt.probabilistic_sharpe_ratio(rets, _DAILY, benchmark_sr=sr_ann * 0.5)
    psr_above = mt.probabilistic_sharpe_ratio(rets, _DAILY, benchmark_sr=sr_ann * 1.5)
    assert psr_below is not None and psr_above is not None
    assert psr_below > 0.5 > psr_above


def test_psr_negative_sharpe_below_half():
    rng = np.random.default_rng(13)
    psr = mt.probabilistic_sharpe_ratio(rng.normal(-0.002, 0.01, 400), _DAILY)
    assert psr is not None and psr < 0.5


def test_psr_short_or_degenerate_samples_are_none():
    rng = np.random.default_rng(21)
    assert mt.probabilistic_sharpe_ratio(rng.normal(0.001, 0.01, 19), _DAILY) is None
    assert mt.probabilistic_sharpe_ratio([], _DAILY) is None
    # 常量收益: 样本标准差退化 → None (不伪造 0)
    assert mt.probabilistic_sharpe_ratio([0.01] * 50, _DAILY) is None


def test_psr_matches_manual_formula():
    # 与 Bailey & López de Prado 闭式手算对照 (固定样本)
    rng = np.random.default_rng(5)
    rets = rng.normal(0.0015, 0.02, 200)
    excess = rets  # rf=0
    sr = float(np.mean(excess) / np.std(excess, ddof=1))
    centered = excess - excess.mean()
    g3 = float(np.mean(centered ** 3) / np.mean(centered ** 2) ** 1.5)
    g4 = float(np.mean(centered ** 4) / np.mean(centered ** 2) ** 2) - 3.0
    n = rets.size
    z = sr * math.sqrt(n - 1) / math.sqrt(1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr ** 2)
    expected = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    got = mt.probabilistic_sharpe_ratio(rets, _DAILY)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# trade_bootstrap_equity_band
# ---------------------------------------------------------------------------


def _band_trades(seed: int = 3, n: int = 40) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.01, 0.05, n)


def test_band_percentiles_monotone_pointwise():
    out = rb.trade_bootstrap_equity_band(_band_trades(), n_boot=2000, seed=0)
    assert out is not None
    p = out["percentiles"]
    for key in ("p05", "p25", "p50", "p75", "p95"):
        assert len(p[key]) == out["n_trades"]
    assert np.all(np.asarray(p["p05"]) <= np.asarray(p["p25"]))
    assert np.all(np.asarray(p["p25"]) <= np.asarray(p["p50"]))
    assert np.all(np.asarray(p["p50"]) <= np.asarray(p["p75"]))
    assert np.all(np.asarray(p["p75"]) <= np.asarray(p["p95"]))


def test_band_deterministic_with_same_seed_and_accepts_list():
    rets = _band_trades()
    a = rb.trade_bootstrap_equity_band(list(map(float, rets)), n_boot=500, seed=11)
    b = rb.trade_bootstrap_equity_band(rets, n_boot=500, seed=11)
    c = rb.trade_bootstrap_equity_band(rets, n_boot=500, seed=12)
    assert a == b  # 同 seed 确定性; list / ndarray 输入等价
    assert a != c


def test_band_insufficient_input_is_none():
    rets = _band_trades()
    assert rb.trade_bootstrap_equity_band(rets[:9], n_boot=1000, seed=0) is None
    assert rb.trade_bootstrap_equity_band(rets, n_boot=99, seed=0) is None
    # 非有限值剔除后不足 10 笔 → None (fail-closed)
    dirty = list(map(float, rets[:12]))
    dirty[3:] = [float("nan")] * 9
    assert rb.trade_bootstrap_equity_band(dirty, n_boot=1000, seed=0) is None


def test_band_p50_final_close_to_original_compounding():
    # 重采样末值 = exp(Σ log(1+r_i)), 顺序无关; 大 n_boot 下 p50 ≈ 原始复利末值
    rets = _band_trades(seed=17, n=60)
    out = rb.trade_bootstrap_equity_band(rets, n_boot=20000, seed=5)
    assert out is not None
    original_final = float(np.prod(1.0 + rets))
    p50_final = out["final_value_percentiles"]["p50"]
    assert abs(p50_final - original_final) <= 0.03 * original_final


def test_band_structure_and_json_safe():
    out = rb.trade_bootstrap_equity_band(_band_trades(seed=23), n_boot=1000, seed=2)
    assert out is not None
    assert out["n_trades"] == 40
    assert out["n_boot"] == 1000
    assert out["seed"] == 2
    # 末值分位与逐点分位带的最后一个元素一致 (同一经验分布)
    for key in ("p05", "p25", "p50", "p75", "p95"):
        assert math.isclose(
            out["final_value_percentiles"][key],
            out["percentiles"][key][-1],
            rel_tol=1e-12,
        )
    # JSON-safe: date→str 类转换由上层处理, 数值必须可序列化
    json.dumps(out)


def test_band_none_not_fake_zero():
    # fail-closed 语义: 不足样本返回 None 而非 0/空带
    assert rb.trade_bootstrap_equity_band([], n_boot=1000, seed=0) is None
