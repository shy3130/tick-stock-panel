"""市场状态 (regime) 条件表现 — 基于基准指数的事后分组分析。

将回测区间按基准指数的市场状态切为四桶 (trend × vol), 分别报告策略与
基准在各桶内的条件表现, 用于回答「策略的超额收益来自哪种市场环境」。

状态定义 (均由基准曲线派生):
- trend: 基准净值 >= 其 60 日滚动均值 (含当日) → above, 否则 below;
- vol:   基准 20 日滚动日收益 std (ddof=1) >= 该滚动序列的**全样本中位数**
         → high, 否则 low。

四桶命名 (trend_high_vol / trend_low_vol / range_high_vol / range_low_vol
的对外名称):
- bull_turbulent / bull_calm / bear_turbulent / bear_calm。

⚠️ 前视说明: vol 阈值取自基准指数事后全样本中位数, 含轻度前视; 该分组
仅用于表现解释与归因诊断, 不得作为交易信号回灌引擎。

fail-closed 约定:
- 两曲线按日期内连接后不足 120 天、曲线为空或无法对齐 → 整体返回 None;
- MA/std 的 warmup 天数 (60 日趋势窗口未就绪) 不进入任何桶,
  各桶 days 之和 + warmup_days = n_days;
- 桶内天数 < 15 时仅保留 days / days_pct, 其余指标一律 None (不伪造);
- 所有输出均为 JSON-safe 标量, 非有限值映射为 None。

n_days 为对齐后的净值点数; 第 i 天的日收益定义为 nav[i]/nav[i-1]-1,
days_pct 按占 n_days (含 warmup) 的比例计。
"""

from __future__ import annotations

import math
from datetime import date, datetime

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from app.backtest.metrics import (
    MetricContext,
    annualized_return,
    annualized_sharpe,
    max_drawdown,
)

__all__ = ["regime_breakdown"]

#: trend 状态的滚动均值窗口 (含当日)
_TREND_WINDOW = 60
#: vol 状态的滚动收益 std 窗口
_VOL_WINDOW = 20
#: 对齐后总天数低于该值 → 整体 None
_MIN_ALIGNED_DAYS = 120
#: 桶内天数低于该值 → 指标全 None
_MIN_BUCKET_DAYS = 15

#: 四桶固定输出顺序
_BUCKET_ORDER = ("bull_turbulent", "bull_calm", "bear_turbulent", "bear_calm")


def _date_key(raw: object) -> str | None:
    """统一日期为可排序的 ISO 字符串; 无法识别返回 None。"""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    if isinstance(raw, str) and raw:
        return raw
    return None


def _parse_curve(curve: list[dict]) -> dict[str, float]:
    """curve → {日期键: 净值}; 剔除日期缺失 / 数值非法 / 非有限的点。"""
    out: dict[str, float] = {}
    if not curve:
        return out
    for point in curve:
        key = _date_key(point.get("date"))
        if key is None:
            continue
        try:
            value = float(point.get("value"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out[key] = value
    return out


def _finite(value: object) -> float | None:
    """非有限 / 非数值统一映射为 None; 否则返回 float。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _total_return(returns: np.ndarray) -> float | None:
    """区间累计收益 = ∏(1+r) - 1; 空序列或非有限结果返回 None。"""
    if returns.size == 0:
        return None
    growth = float(np.prod(1.0 + returns))
    if growth <= 0.0:
        return -1.0
    return _finite(growth - 1.0)


def _bucket_stats(
    mask: np.ndarray,
    rets_s: np.ndarray,
    rets_b: np.ndarray,
    n_days: int,
    context: MetricContext,
) -> dict:
    """单桶统计; 天数不足时仅保留 days / days_pct, 指标不伪造。"""
    idx = np.flatnonzero(mask)
    days = int(idx.size)
    stats: dict = {
        "days": days,
        "days_pct": days / n_days,
        "strategy_total_return": None,
        "strategy_annualized_return": None,
        "strategy_sharpe": None,
        "strategy_max_drawdown": None,
        "benchmark_total_return": None,
        "excess_total_return": None,
    }
    if days < _MIN_BUCKET_DAYS:
        return stats
    # 第 i 天的收益位于 rets[i-1]; 分类日 i >= 59 保证索引合法
    s_ret = rets_s[idx - 1]
    b_ret = rets_b[idx - 1]
    stats["strategy_total_return"] = _total_return(s_ret)
    stats["strategy_annualized_return"] = _finite(annualized_return(s_ret, context))
    stats["strategy_sharpe"] = _finite(annualized_sharpe(s_ret, context))
    stats["strategy_max_drawdown"] = _finite(max_drawdown(s_ret))
    stats["benchmark_total_return"] = _total_return(b_ret)
    if stats["strategy_total_return"] is not None and stats["benchmark_total_return"] is not None:
        stats["excess_total_return"] = _finite(
            stats["strategy_total_return"] - stats["benchmark_total_return"]
        )
    return stats


def regime_breakdown(
    strategy_curve: list[dict],
    benchmark_curve: list[dict],
    context: MetricContext,
) -> dict | None:
    """按基准定义的市场状态分桶统计策略条件表现。

    参数
    ----
    strategy_curve / benchmark_curve: ``[{date, value}, ...]`` 净值曲线,
        date 接受 ISO 字符串或 ``date``/``datetime``; 按日期内连接对齐。
    context: 年化与频率口径 (periods_per_year 由 return_frequency 派生)。

    返回
    ----
    ``{n_days, warmup_days, buckets, definitions, metric_context}``;
    数据不足 (对齐 < 120 天 / 无法对齐) 返回 None。

    注意: vol 阈值基于基准全样本中位数, 属事后统计 (轻度前视), 仅用于
    分组解释, 不构成交易信号。
    """
    strat = _parse_curve(strategy_curve)
    bench = _parse_curve(benchmark_curve)
    common = sorted(set(strat) & set(bench))
    n_days = len(common)
    if n_days < _MIN_ALIGNED_DAYS:
        return None

    nav_s = np.asarray([strat[d] for d in common], dtype=float)
    nav_b = np.asarray([bench[d] for d in common], dtype=float)
    # rets_*[i-1] 即对齐后第 i 天的日收益 (i 从 0 计)
    rets_s = nav_s[1:] / nav_s[:-1] - 1.0
    rets_b = nav_b[1:] / nav_b[:-1] - 1.0

    # trend: 60 日滚动均值 (含当日), 定义于 nav 索引 >= 59; warmup 期为 NaN
    ma60 = np.full(n_days, np.nan)
    ma60[_TREND_WINDOW - 1 :] = sliding_window_view(nav_b, _TREND_WINDOW).mean(axis=1)
    above = nav_b >= ma60  # 与 NaN 比较为 False, 由 classifiable 统一屏蔽

    # vol: 20 日滚动日收益 std (ddof=1), 定义于 nav 索引 >= 20
    rolling_std = sliding_window_view(rets_b, _VOL_WINDOW).std(axis=1, ddof=context.std_ddof)
    # 全样本中位数阈值 (事后统计, 见模块 docstring 前视说明)
    vol_median = float(np.median(rolling_std))
    high = np.zeros(n_days, dtype=bool)
    high[_VOL_WINDOW:] = rolling_std >= vol_median

    # trend 窗口 (60 日) 比 vol 窗口 (20 日) 长, warmup 由 trend 决定
    classifiable = ~np.isnan(ma60)
    warmup_days = int(np.count_nonzero(~classifiable))

    masks = {
        "bull_turbulent": classifiable & above & high,
        "bull_calm": classifiable & above & ~high,
        "bear_turbulent": classifiable & ~above & high,
        "bear_calm": classifiable & ~above & ~high,
    }
    buckets = {
        name: _bucket_stats(mask, rets_s, rets_b, n_days, context)
        for name, mask in masks.items()
    }

    return {
        "n_days": n_days,
        "warmup_days": warmup_days,
        "buckets": buckets,
        "definitions": {
            "trend": "基准净值 vs 60日均值",
            "vol": "基准20日滚动波动 vs 全样本中位数",
        },
        "metric_context": context.to_dict(),
    }
