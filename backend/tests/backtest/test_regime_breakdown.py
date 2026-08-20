"""regime_breakdown 单元测试 — 市场状态条件表现 (A4)。

合成基准曲线: 前段单调上行 + 低波动, 后段下行 + 高波动; 桶归属用测试侧
独立实现的逐日循环分类器对账, 指标数值与 metrics 直接计算结果核对。
"""

import json
import math
import statistics
from datetime import date, timedelta

import numpy as np

from app.backtest import metrics as mt
from app.backtest.regime_breakdown import regime_breakdown

_CONTEXT = mt.MetricContext("daily")

_BUCKET_NAMES = ("bull_turbulent", "bull_calm", "bear_turbulent", "bear_calm")


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------


def _dates(n: int, start: date = date(2022, 1, 3)) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _curve_from_returns(returns: list[float], dates: list[str], start: float = 1.0) -> list[dict]:
    """由日收益序列构造 [{date, value}] 净值曲线 (点数 = 收益数 + 1)。"""
    nav = [start]
    for r in returns:
        nav.append(nav[-1] * (1.0 + r))
    return [{"date": d, "value": v} for d, v in zip(dates, nav)]


def _make_main_fixture() -> tuple[list[dict], list[dict]]:
    """前段低波上行 + 后段高波下行的基准; 策略为独立的随机游走。"""
    rng_bench = np.random.default_rng(7)
    calm_up = [0.001 + 0.0004 * math.sin(i * 0.7) for i in range(140)]
    turbulent_down = list(-0.005 + rng_bench.normal(0.0, 0.02, size=170))
    bench_rets = calm_up + turbulent_down

    rng_strat = np.random.default_rng(21)
    strat_rets = list(0.0008 + rng_strat.normal(0.0, 0.01, size=len(bench_rets)))

    dates = _dates(len(bench_rets) + 1)
    return _curve_from_returns(strat_rets, dates), _curve_from_returns(bench_rets, dates)


def _independent_classify(bench_curve: list[dict]) -> dict[str, list[int]]:
    """测试侧独立实现: 纯 Python 循环重算 trend/vol 状态, 返回 {桶: [日索引]}。

    与被测模块刻意采用不同写法 (循环 + statistics), 用于交叉对账。
    """
    nav = [float(p["value"]) for p in bench_curve]
    n = len(nav)
    rets = [nav[i] / nav[i - 1] - 1.0 for i in range(1, n)]
    stds: dict[int, float] = {}
    for i in range(20, n):
        window = rets[i - 20 : i]
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
        stds[i] = math.sqrt(var)
    median = statistics.median(stds.values())

    out: dict[str, list[int]] = {name: [] for name in _BUCKET_NAMES}
    for i in range(59, n):
        above = nav[i] >= sum(nav[i - 59 : i + 1]) / 60
        high = stds[i] >= median
        if above and high:
            out["bull_turbulent"].append(i)
        elif above:
            out["bull_calm"].append(i)
        elif high:
            out["bear_turbulent"].append(i)
        else:
            out["bear_calm"].append(i)
    return out


# ---------------------------------------------------------------------------
# 桶归属与天数
# ---------------------------------------------------------------------------


def test_bucket_days_match_independent_classifier():
    strat_curve, bench_curve = _make_main_fixture()
    result = regime_breakdown(strat_curve, bench_curve, _CONTEXT)
    assert result is not None

    expected = _independent_classify(bench_curve)
    for name in _BUCKET_NAMES:
        assert result["buckets"][name]["days"] == len(expected[name]), name

    # 前段单调上行 + 低波动 → bull_calm 占主导; 后段下行 → bear 侧有大量天数
    assert result["buckets"]["bull_calm"]["days"] >= 60
    bear_days = result["buckets"]["bear_turbulent"]["days"] + result["buckets"]["bear_calm"]["days"]
    assert bear_days >= 60


def test_warmup_excluded_and_accounting_closes():
    strat_curve, bench_curve = _make_main_fixture()
    result = regime_breakdown(strat_curve, bench_curve, _CONTEXT)
    assert result is not None

    # 60 日均值窗口 (含当日) 前 59 天无法分类 → warmup
    assert result["warmup_days"] == 59
    assert result["n_days"] == len(bench_curve)
    total_bucket_days = sum(result["buckets"][n]["days"] for n in _BUCKET_NAMES)
    assert total_bucket_days + result["warmup_days"] == result["n_days"]
    # days_pct 以 n_days (含 warmup) 为分母
    total_pct = sum(result["buckets"][n]["days_pct"] for n in _BUCKET_NAMES)
    assert math.isclose(
        total_pct, total_bucket_days / result["n_days"], abs_tol=1e-12
    )


# ---------------------------------------------------------------------------
# 指标与 metrics 直接计算对账 (手工对账)
# ---------------------------------------------------------------------------


def test_bull_calm_metrics_reconcile_with_direct_computation():
    strat_curve, bench_curve = _make_main_fixture()
    result = regime_breakdown(strat_curve, bench_curve, _CONTEXT)
    assert result is not None

    name = "bull_calm"
    bucket = result["buckets"][name]
    idxs = np.asarray(_independent_classify(bench_curve)[name])
    assert idxs.size >= 15  # 大桶才有指标

    nav_s = np.asarray([p["value"] for p in strat_curve])
    nav_b = np.asarray([p["value"] for p in bench_curve])
    s_ret = nav_s[idxs] / nav_s[idxs - 1] - 1.0
    b_ret = nav_b[idxs] / nav_b[idxs - 1] - 1.0

    assert math.isclose(bucket["strategy_total_return"], float(np.prod(1.0 + s_ret) - 1.0), rel_tol=1e-12)
    assert math.isclose(bucket["strategy_annualized_return"], mt.annualized_return(s_ret, _CONTEXT), rel_tol=1e-12)
    assert math.isclose(bucket["strategy_sharpe"], mt.annualized_sharpe(s_ret, _CONTEXT), rel_tol=1e-12)
    assert math.isclose(bucket["strategy_max_drawdown"], mt.max_drawdown(s_ret), rel_tol=1e-12)
    assert math.isclose(bucket["benchmark_total_return"], float(np.prod(1.0 + b_ret) - 1.0), rel_tol=1e-12)
    expected_excess = bucket["strategy_total_return"] - bucket["benchmark_total_return"]
    assert math.isclose(bucket["excess_total_return"], expected_excess, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# 小桶 (days < 15) 指标不伪造
# ---------------------------------------------------------------------------


def _make_small_bucket_fixture() -> tuple[list[dict], list[dict]]:
    """平稳上行后接短暂高波脉冲, 随后陡降 → bull_turbulent 天数很少。"""
    rng = np.random.default_rng(11)
    calm_up = [0.001] * 139
    burst = [0.06, -0.04, 0.06, -0.04, 0.06, -0.04, 0.06, -0.04]
    plunge = list(-0.02 + rng.normal(0.0, 0.012, size=180))
    bench_rets = calm_up + burst + plunge
    rng_strat = np.random.default_rng(23)
    strat_rets = list(0.0005 + rng_strat.normal(0.0, 0.008, size=len(bench_rets)))
    dates = _dates(len(bench_rets) + 1)
    return _curve_from_returns(strat_rets, dates), _curve_from_returns(bench_rets, dates)


def test_small_bucket_metrics_are_none_but_days_kept():
    strat_curve, bench_curve = _make_small_bucket_fixture()
    result = regime_breakdown(strat_curve, bench_curve, _CONTEXT)
    assert result is not None

    small = result["buckets"]["bull_turbulent"]
    expected_days = len(_independent_classify(bench_curve)["bull_turbulent"])
    assert small["days"] == expected_days
    assert 1 <= small["days"] < 15  # 确为小桶, 而非构造失效
    for key in (
        "strategy_total_return",
        "strategy_annualized_return",
        "strategy_sharpe",
        "strategy_max_drawdown",
        "benchmark_total_return",
        "excess_total_return",
    ):
        assert small[key] is None, key
    assert small["days_pct"] == small["days"] / result["n_days"]

    # 其它大桶指标正常 → None 确因样本小而非整体失败
    big = [result["buckets"][n] for n in _BUCKET_NAMES if result["buckets"][n]["days"] >= 15]
    assert big and all(b["strategy_total_return"] is not None for b in big)


# ---------------------------------------------------------------------------
# 数据不足 / 无法对齐 → None
# ---------------------------------------------------------------------------


def test_too_few_aligned_days_returns_none():
    rng = np.random.default_rng(3)
    rets = list(0.001 + rng.normal(0.0, 0.002, size=118))  # 119 个净值点 < 120
    dates = _dates(len(rets) + 1)
    curve = _curve_from_returns(rets, dates)
    assert regime_breakdown(curve, curve, _CONTEXT) is None


def test_empty_or_unalignable_curves_return_none():
    strat_curve, bench_curve = _make_main_fixture()
    assert regime_breakdown([], bench_curve, _CONTEXT) is None
    assert regime_breakdown(strat_curve, [], _CONTEXT) is None
    # 日期完全不相交 → 无法对齐
    other = [{"date": d, "value": 1.0} for d in _dates(200, start=date(1999, 1, 1))]
    assert regime_breakdown(strat_curve, other, _CONTEXT) is None


def test_nonfinite_points_dropped_before_alignment():
    strat_curve, bench_curve = _make_main_fixture()
    polluted = list(bench_curve)
    polluted[5] = {"date": polluted[5]["date"], "value": float("nan")}
    polluted[6] = {"date": polluted[6]["date"], "value": float("inf")}
    result = regime_breakdown(strat_curve, polluted, _CONTEXT)
    assert result is not None
    assert result["n_days"] == len(bench_curve) - 2


# ---------------------------------------------------------------------------
# 对齐 / 输出形状
# ---------------------------------------------------------------------------


def test_inner_join_alignment_on_dates():
    strat_curve, bench_curve = _make_main_fixture()
    # 策略多出 10 个前置日期, 基准多出 10 个后置日期 → 内连接取交集
    extra_head = [
        {"date": (date(2021, 10, 1) + timedelta(days=i)).isoformat(), "value": 1.0}
        for i in range(10)
    ]
    extra_tail = [
        {"date": (date(2026, 6, 1) + timedelta(days=i)).isoformat(), "value": 2.0}
        for i in range(10)
    ]
    result = regime_breakdown(extra_head + strat_curve, bench_curve + extra_tail, _CONTEXT)
    assert result is not None
    assert result["n_days"] == len(bench_curve)


def test_accepts_date_objects_equivalently():
    strat_curve, bench_curve = _make_main_fixture()
    as_str = regime_breakdown(strat_curve, bench_curve, _CONTEXT)
    strat_obj = [
        {"date": date.fromisoformat(p["date"]), "value": p["value"]} for p in strat_curve
    ]
    bench_obj = [
        {"date": date.fromisoformat(p["date"]), "value": p["value"]} for p in bench_curve
    ]
    as_obj = regime_breakdown(strat_obj, bench_obj, _CONTEXT)
    assert as_str == as_obj


def test_output_shape_definitions_and_json_safe():
    strat_curve, bench_curve = _make_main_fixture()
    result = regime_breakdown(strat_curve, bench_curve, _CONTEXT)
    assert result is not None

    assert set(result) == {"n_days", "warmup_days", "buckets", "definitions", "metric_context"}
    assert set(result["buckets"]) == set(_BUCKET_NAMES)
    assert result["definitions"] == {
        "trend": "基准净值 vs 60日均值",
        "vol": "基准20日滚动波动 vs 全样本中位数",
    }
    assert result["metric_context"] == _CONTEXT.to_dict()
    # 全结构可直接 JSON 序列化 (无 numpy 标量 / date 残留)
    json.dumps(result, allow_nan=False)
