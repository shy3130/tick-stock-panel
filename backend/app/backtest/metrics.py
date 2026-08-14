"""回测绩效与风险指标 — 纯 numpy / 标准库实现。

移植来源
--------
上游 Vibe-Trading 的回测指标增强散见于两个 skill:

* ``agent/src/skills/performance-attribution/SKILL.md`` —— "Risk-Adjusted
  Performance Metrics" 表给出 Sharpe / Sortino / Calmar / Information Ratio /
  Treynor 的口径与门槛, 是本模块 Sortino、Calmar 的直接来源。
* ``agent/src/skills/quant-statistics/SKILL.md`` —— "Bootstrap Methods" 节的
  非参数百分位置信区间 (``bootstrap_statistic`` / ``bootstrap_sharpe``), 是本
  模块 :func:`bootstrap_confidence_interval` 的直接来源。

其余指标 (Omega、tail ratio、profit factor、payoff ratio、expectancy、
win/loss streak、exposure、trade duration、Ulcer Index、VaR、CVaR) 为补齐
"Risk-Adjusted Performance Metrics" 表的标准回测风险/绩效工具集, 口径取
业界通用定义 (见各函数 docstring)。

设计约束 (与 ``quant_stats.py`` / ``attribution.py`` 一致)
---------------------------------------------------------
* **纯计算**: 只接受 numpy 兼容输入, 无网络 / 磁盘 I/O, 不输出任何交易方向 /
  仓位 / 订单建议。
* **不新增依赖**: 仅 numpy + 标准库 (``math``); 不导入 pandas / scipy /
  statsmodels。
* **fail-soft**: 输入为空 / 样本不足 / 分母为零 → 统一返回 ``None`` (标量函数)
  或 ``status="insufficient_data"`` (聚合函数), **不抛异常**。
* **非有限值**: 输入序列中的 nan/inf 按 SKILL 的 ``dropna`` 语义剔除; 所有输出
  数值经 :func:`_finite_or_none` 映射, nan/inf → None。
* **口径中性**: 比率的年化由 ``periods_per_year`` 控制 (日频 252 / 月频 12),
  默认不假设频率; bootstrap 支持确定性 ``seed`` 以保证可复现。
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np

__all__ = [
    "bootstrap_confidence_interval",
    "calmar_ratio",
    "conditional_value_at_risk",
    "downside_deviation",
    "expectancy",
    "exposure",
    "max_drawdown",
    "omega_ratio",
    "payoff_ratio",
    "performance_metrics",
    "profit_factor",
    "sortino_ratio",
    "tail_ratio",
    "trade_duration_stats",
    "ulcer_index",
    "value_at_risk",
    "win_loss_streak",
]


# ---------------------------------------------------------------------------
# 通用 helpers
# ---------------------------------------------------------------------------


def _finite_or_none(value: object) -> float | None:
    """非有限值 (含 None / 非数值) 统一映射为 None; 否则返回 float。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clean_series(series) -> np.ndarray | None:
    """转 float 一维数组并剔除非有限值 (SKILL 的 ``dropna`` 语义); 全空返回 None。"""
    if series is None:
        return None
    try:
        arr = np.asarray(series, dtype=float).ravel()
    except (TypeError, ValueError):
        return None
    arr = arr[np.isfinite(arr)]
    return arr if arr.size > 0 else None


# ---------------------------------------------------------------------------
# 收益路径类指标 (输入: 周期收益率序列)
# ---------------------------------------------------------------------------


def downside_deviation(returns, threshold: float = 0.0) -> float | None:
    """目标下行偏差 (Sortino 分母)。

    口径::

        downside = sqrt( mean( min(r_t - T, 0)^2 ) )

    即所有 **低于门槛 T** 的偏差取平方后的均方根 (含零项, 非条件标准差)。
    返回值为 **每周期** 下行偏差; 年化时乘 ``sqrt(periods_per_year)``。

    Args:
        returns: 周期收益率序列。
        threshold: 最低可接受收益 (MAR / 目标), 默认 0。

    Returns:
        每周期下行偏差; 无下行 (全高于门槛) 返回 ``0.0``; 输入为空返回 ``None``。
    """
    arr = _clean_series(returns)
    if arr is None:
        return None
    shortfall = np.minimum(arr - float(threshold), 0.0)
    return float(np.sqrt(np.mean(shortfall ** 2)))


def sortino_ratio(
    returns,
    periods_per_year: int = 252,
    risk_free: float = 0.0,
    threshold: float | None = None,
) -> float | None:
    """Sortino 比率 (SKILL "Risk-Adjusted Performance Metrics" 表)。

    口径::

        Sortino = (mean(r - rf)) * P / (downside_dev(r, MAR) * sqrt(P))

    其中 ``P = periods_per_year``; 分子为年化超额收益, 分母为年化下行偏差;
    ``MAR`` (最低可接受收益) 默认等于 ``risk_free``, 可经 ``threshold`` 覆盖。

    Args:
        returns: 周期收益率序列。
        periods_per_year: 年化系数 (日频 252 / 月频 12)。
        risk_free: 每周期无风险利率。
        threshold: 显式 MAR; ``None`` 时取 ``risk_free``。

    Returns:
        Sortino 比率; 无下行风险 (分母为 0) 或输入不足返回 ``None``。
    """
    arr = _clean_series(returns)
    if arr is None or arr.size == 0:
        return None
    mar = float(risk_free) if threshold is None else float(threshold)
    excess = arr - float(risk_free)
    ann_excess = float(np.mean(excess)) * float(periods_per_year)
    shortfall = np.minimum(arr - mar, 0.0)
    dd = float(np.sqrt(np.mean(shortfall ** 2)))
    if dd <= 0.0:
        return None
    return ann_excess / (dd * math.sqrt(float(periods_per_year)))


def omega_ratio(returns, threshold: float = 0.0) -> float | None:
    """Omega 比率。

    口径::

        Omega = sum(r_t - T for r_t > T) / sum(T - r_t for r_t < T)

    即门槛 ``T`` 之上累计收益对之下累计亏损的比值; 越大越好, =1 时盈亏平衡。

    Returns:
        Omega; 无亏损 (分母为 0) 返回 ``None``; 无盈利且无亏损返回 ``None``;
        输入为空返回 ``None``。
    """
    arr = _clean_series(returns)
    if arr is None:
        return None
    diff = arr - float(threshold)
    gains = float(diff[diff > 0.0].sum())
    losses = float(-diff[diff < 0.0].sum())
    if losses <= 0.0:
        return None
    return gains / losses


def tail_ratio(returns) -> float | None:
    """尾比率: 右尾 (95 分位) 绝对值 / 左尾 (5 分位) 绝对值。

    口径:: 

        tail_ratio = |percentile(r, 95)| / |percentile(r, 5)|

    衡量"好收益的量级"相对"坏收益的量级"; >1 表示右尾厚于左尾。

    Returns:
        尾比率; 左尾为 0 或样本不足返回 ``None``。
    """
    arr = _clean_series(returns)
    if arr is None or arr.size < 2:
        return None
    p95 = float(np.percentile(arr, 95.0))
    p5 = float(np.percentile(arr, 5.0))
    if abs(p5) < 1e-15:
        return None
    return abs(p95) / abs(p5)


def max_drawdown(returns) -> float | None:
    """最大回撤 (路径型)。

    口径::

        wealth_t = cumprod(1 + r_t)
        dd_t     = wealth_t / running_max(wealth) - 1
        max_dd   = min(dd_t)   # <= 0

    返回值为 **负数或 0** (例如 -0.15 表示 15% 回撤), 与 ``engine.py`` 一致。

    Returns:
        最大回撤 (<=0); 输入为空返回 ``None``。
    """
    arr = _clean_series(returns)
    if arr is None or arr.size == 0:
        return None
    # 净值路径含起点 1.0, 保证从初始最高点出发的回撤被计入 (与 engine.py 一致)。
    wealth = np.empty(arr.size + 1, dtype=float)
    wealth[0] = 1.0
    wealth[1:] = np.cumprod(1.0 + arr)
    peaks = np.maximum.accumulate(wealth)
    dd = wealth / peaks - 1.0
    mdd = float(np.min(dd))
    # 单期收益 < -1 (带杠杆的极端亏损) 会使 wealth 翻负; 钳到 -1 (全损) 保持语义。
    return max(mdd, -1.0)


def calmar_ratio(returns, periods_per_year: int = 252) -> float | None:
    """Calmar 比率 (SKILL "Risk-Adjusted Performance Metrics" 表)。

    口径::

        Calmar = annualized_return / |max_drawdown|

    年化收益用 CAGR: ``(1 + total) ** (P / n) - 1``。

    Returns:
        Calmar; 无回撤 (|max_dd| -> 0) 或输入不足返回 ``None``。
    """
    arr = _clean_series(returns)
    if arr is None or arr.size == 0:
        return None
    mdd = max_drawdown(arr)
    if mdd is None or abs(mdd) < 1e-12:
        return None
    total = float(np.prod(1.0 + arr)) - 1.0
    n = arr.size
    if total > -1.0 and n > 0:
        ann = (1.0 + total) ** (float(periods_per_year) / float(n)) - 1.0
    else:
        ann = total
    return float(ann) / abs(mdd)


def ulcer_index(returns) -> float | None:
    """Ulcer 指数: 回撤深度的均方根。

    口径::

        dd_t = wealth_t / running_max(wealth) - 1
        UI   = sqrt( mean(dd_t^2) )

    同时惩罚回撤 **深度与持续时间** (Martin 指数)。

    Returns:
        Ulcer 指数 (>=0); 输入为空返回 ``None``。
    """
    arr = _clean_series(returns)
    if arr is None or arr.size == 0:
        return None
    wealth = np.empty(arr.size + 1, dtype=float)
    wealth[0] = 1.0
    wealth[1:] = np.cumprod(1.0 + arr)
    peaks = np.maximum.accumulate(wealth)
    dd = wealth / peaks - 1.0
    return float(np.sqrt(np.mean(dd ** 2)))


def value_at_risk(returns, alpha: float = 0.05) -> float | None:
    """历史 VaR (经验分位数)。

    口径::

        VaR_alpha = percentile(r, alpha * 100)

    ``alpha=0.05`` 取 5% 分位 (一个负数, 代表 5% 概率的最坏日收益)。

    Returns:
        VaR; 输入为空或 ``alpha`` 非法返回 ``None``。
    """
    if not (0.0 < alpha < 1.0):
        return None
    arr = _clean_series(returns)
    if arr is None or arr.size == 0:
        return None
    return float(np.percentile(arr, alpha * 100.0))


def conditional_value_at_risk(returns, alpha: float = 0.05) -> float | None:
    """条件 VaR / 预期短缺 (Expected Shortfall)。

    口径::

        VaR  = percentile(r, alpha * 100)
        CVaR = mean(r_t for r_t <= VaR)

    即最坏 ``alpha`` 尾部的平均收益, 比 VaR 更稳健地度量尾部风险。

    Returns:
        CVaR; 输入为空或 ``alpha`` 非法返回 ``None``。
    """
    if not (0.0 < alpha < 1.0):
        return None
    arr = _clean_series(returns)
    if arr is None or arr.size == 0:
        return None
    var = float(np.percentile(arr, alpha * 100.0))
    tail = arr[arr <= var]
    if tail.size == 0:
        return var
    return float(np.mean(tail))


# ---------------------------------------------------------------------------
# 交易类指标 (输入: 逐笔盈亏 / 持仓时长 / 持仓标记)
# ---------------------------------------------------------------------------


def profit_factor(pnls) -> float | None:
    """盈亏因子: 总盈利 / 总亏损(绝对值)。

    口径::

        PF = sum(pnl for pnl > 0) / |sum(pnl for pnl < 0)|

    >1 盈利, <1 亏损。

    Returns:
        盈亏因子; 无亏损 (分母为 0) 返回 ``None``; 输入为空返回 ``None``。
    """
    arr = _clean_series(pnls)
    if arr is None or arr.size == 0:
        return None
    gains = float(arr[arr > 0.0].sum())
    losses = float(-arr[arr < 0.0].sum())
    if losses <= 0.0:
        return None
    return gains / losses


def payoff_ratio(pnls) -> float | None:
    """盈亏比 (单笔): 平均盈利 / 平均亏损(绝对值)。

    口径::

        payoff = mean(pnl | pnl > 0) / |mean(pnl | pnl < 0)|

    Returns:
        盈亏比; 无盈利或无亏损返回 ``None``。
    """
    arr = _clean_series(pnls)
    if arr is None:
        return None
    wins = arr[arr > 0.0]
    losses = arr[arr < 0.0]
    if wins.size == 0 or losses.size == 0:
        return None
    avg_loss = abs(float(np.mean(losses)))
    if avg_loss < 1e-15:
        return None
    return float(np.mean(wins)) / avg_loss


def expectancy(pnls) -> float | None:
    """单笔期望值: 每笔交易的平均盈亏。

    口径::

        expectancy = mean(pnl)
                   = win_rate * avg_win - loss_rate * |avg_loss|

    (两种写法数学等价; 这里直接取均值, 数值更稳。)

    Returns:
        每笔期望盈亏; 输入为空返回 ``None``。
    """
    arr = _clean_series(pnls)
    if arr is None or arr.size == 0:
        return None
    return float(np.mean(arr))


def win_loss_streak(pnls) -> dict:
    """最大连胜 / 连亏及胜负计数。

    口径: 按交易顺序统计连续盈利 (pnl>0) 与连续亏损 (pnl<0) 的最大长度;
    pnl==0 视为平局, 中断当前连胜/连亏。

    Returns:
        ``{"max_win_streak", "max_loss_streak", "n_wins", "n_losses"}``;
        输入为空时计数均为 0。
    """
    arr = _clean_series(pnls)
    if arr is None or arr.size == 0:
        return {"max_win_streak": 0, "max_loss_streak": 0, "n_wins": 0, "n_losses": 0}
    max_win = max_loss = cur_win = cur_loss = 0
    for s in np.sign(arr):
        if s > 0:
            cur_win += 1
            cur_loss = 0
            max_win = max(max_win, cur_win)
        elif s < 0:
            cur_loss += 1
            cur_win = 0
            max_loss = max(max_loss, cur_loss)
        else:
            cur_win = 0
            cur_loss = 0
    return {
        "max_win_streak": int(max_win),
        "max_loss_streak": int(max_loss),
        "n_wins": int((arr > 0.0).sum()),
        "n_losses": int((arr < 0.0).sum()),
    }


def trade_duration_stats(durations) -> dict:
    """持仓时长统计 (avg / median / min / max / n)。

    Args:
        durations: 逐笔持仓周期数 (天数 / bar 数等, 口径由调用方决定)。

    Returns:
        统计字典; 输入为空时数值字段为 ``None``, ``n=0``。
    """
    arr = _clean_series(durations)
    if arr is None or arr.size == 0:
        return {"avg": None, "median": None, "min": None, "max": None, "n": 0}
    return {
        "avg": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "n": int(arr.size),
    }


def exposure(positions) -> float | None:
    """市场暴露度: 持仓标记的均值。

    输入为逐周期 (或逐笔) 的持仓标记:

    * **二值 (0/1)**: 返回 **在市时间占比** (time-in-market)。
    * **权重 ([0,1] 或更高)**: 返回 **平均资金占用比例**。

    口径:: ``exposure = mean(positions)``。

    Returns:
        暴露度; 输入为空返回 ``None``。
    """
    arr = _clean_series(positions)
    if arr is None or arr.size == 0:
        return None
    return float(np.mean(arr))


# ---------------------------------------------------------------------------
# Bootstrap 置信区间 (来源: quant-statistics SKILL "Bootstrap Methods")
# ---------------------------------------------------------------------------


def bootstrap_confidence_interval(
    data,
    statistic: Callable[[np.ndarray], float] = np.mean,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    seed: int | None = None,
) -> dict:
    """非参数百分位 Bootstrap 置信区间。

    口径 (SKILL "Nonparametric Bootstrap")::

        对原始样本做 n_bootstrap 次有放回重采样 (size=len(data)),
        对每次重采样计算 statistic, 取经验分布的 (alpha/2, 1-alpha/2) 百分位
        作为置信区间下/上限。

    Args:
        data: 原始样本序列 (如收益率)。
        statistic: 统计量函数 ``f(arr) -> float``, 默认 ``np.mean``;
            也可传 Sharpe / Sortino / max_drawdown 等本模块函数。
        n_bootstrap: 重采样次数。
        confidence: 置信水平 (如 0.95)。
        seed: 随机种子; 给定即 **确定性可复现**。

    Returns:
        ``{"status", "point_estimate", "bootstrap_mean", "bootstrap_std",
        "ci_lower", "ci_upper", "confidence", "n_bootstrap", "n"}``;
        样本不足 / 重采样统计量全部非有限时 ``status="insufficient_data"`` 且
        数值字段为 ``None``。
    """
    arr = _clean_series(data)
    insufficient = {
        "status": "insufficient_data",
        "point_estimate": None,
        "bootstrap_mean": None,
        "bootstrap_std": None,
        "ci_lower": None,
        "ci_upper": None,
        "confidence": float(confidence),
        "n_bootstrap": 0,
        "n": int(arr.size) if arr is not None else 0,
    }
    if arr is None or arr.size < 2 or n_bootstrap < 1:
        return insufficient
    if not (0.0 < confidence < 1.0):
        return insufficient

    rng = np.random.default_rng(seed)
    n = arr.size
    # 一次性生成重采样索引矩阵, 再逐行应用统计量 (统计量为任意可调用对象, 无法向量化)。
    idx = rng.integers(0, n, size=(int(n_bootstrap), n))
    samples = np.empty(int(n_bootstrap), dtype=float)
    for i in range(int(n_bootstrap)):
        try:
            samples[i] = float(statistic(arr[idx[i]]))
        except (ValueError, FloatingPointError, ZeroDivisionError):
            samples[i] = np.nan

    valid = samples[np.isfinite(samples)]
    if valid.size == 0:
        insufficient["n_bootstrap"] = 0
        return insufficient

    alpha = 1.0 - float(confidence)
    lo = float(np.percentile(valid, alpha / 2.0 * 100.0))
    hi = float(np.percentile(valid, (1.0 - alpha / 2.0) * 100.0))
    point = _safe_statistic(statistic, arr)
    return {
        "status": "ok",
        "point_estimate": point,
        "bootstrap_mean": float(np.mean(valid)),
        "bootstrap_std": float(np.std(valid)),
        "ci_lower": lo,
        "ci_upper": hi,
        "confidence": float(confidence),
        "n_bootstrap": int(valid.size),
        "n": int(n),
    }


def _safe_statistic(statistic: Callable[[np.ndarray], float], arr: np.ndarray) -> float | None:
    """对原始样本应用统计量, 异常 / 非有限一律映射为 None。"""
    try:
        return _finite_or_none(statistic(arr))
    except (ValueError, FloatingPointError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# 聚合入口
# ---------------------------------------------------------------------------


def performance_metrics(
    returns=None,
    pnls=None,
    durations=None,
    positions=None,
    periods_per_year: int = 252,
    risk_free: float = 0.0,
    threshold: float = 0.0,
) -> dict:
    """一次性计算回测绩效 / 风险指标全集。

    分两组:

    * **收益路径类** (来自 ``returns``, 周期收益率序列): Sortino、Omega、
      tail ratio、最大回撤、Calmar、Ulcer、VaR、CVaR、下行偏差。
    * **交易类** (来自 ``pnls`` / ``durations`` / ``positions``, 均可缺省):
      盈亏因子、盈亏比、期望值、连胜/连亏、持仓时长、暴露度。

    任何输入缺省时对应字段为 ``None``; 全部缺省返回 ``status="insufficient_data"``。

    Args:
        returns: 周期收益率序列 (路径类指标的输入)。
        pnls: 逐笔盈亏序列 (交易类指标输入)。
        durations: 逐笔持仓周期数。
        positions: 逐周期/逐笔持仓标记 (0/1 或权重)。
        periods_per_year: 年化系数 (路径类比率年化用)。
        risk_free: 每周期无风险利率 (Sortino 分子)。
        threshold: Sortino 的 MAR (``None`` 时取 ``risk_free``); Omega 的门槛。

    Returns:
        指标字典; 所有数值字段经 fail-soft 处理, 未定义时为 ``None``。
    """
    out: dict = {"status": "insufficient_data"}
    has_any = False

    rarr = _clean_series(returns)
    if rarr is not None and rarr.size > 0:
        has_any = True
        out.update(
            {
                "downside_deviation": _finite_or_none(downside_deviation(rarr, threshold)),
                "sortino": _finite_or_none(
                    sortino_ratio(rarr, periods_per_year, risk_free, threshold)
                ),
                "omega": _finite_or_none(omega_ratio(rarr, threshold)),
                "tail_ratio": _finite_or_none(tail_ratio(rarr)),
                "max_drawdown": _finite_or_none(max_drawdown(rarr)),
                "calmar": _finite_or_none(calmar_ratio(rarr, periods_per_year)),
                "ulcer_index": _finite_or_none(ulcer_index(rarr)),
                "value_at_risk": _finite_or_none(value_at_risk(rarr)),
                "conditional_value_at_risk": _finite_or_none(conditional_value_at_risk(rarr)),
            }
        )

    parr = _clean_series(pnls)
    if parr is not None and parr.size > 0:
        has_any = True
        out.update(
            {
                "profit_factor": _finite_or_none(profit_factor(parr)),
                "payoff_ratio": _finite_or_none(payoff_ratio(parr)),
                "expectancy": _finite_or_none(expectancy(parr)),
                "win_loss_streak": win_loss_streak(parr),
            }
        )

    darr = _clean_series(durations)
    if darr is not None and darr.size > 0:
        has_any = True
        out["trade_duration"] = trade_duration_stats(darr)

    exarr = _clean_series(positions)
    if exarr is not None and exarr.size > 0:
        has_any = True
        out["exposure"] = _finite_or_none(exposure(exarr))

    if has_any:
        out["status"] = "ok"
    return out
