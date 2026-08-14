"""量化研究统计工具 — 纯 numpy / 标准库实现。

移植来源
--------
``../Vibe-Trading/agent/src/skills/quant-statistics/SKILL.md`` —— 上游用
``statsmodels`` / ``arch`` 提供统计检验与波动率建模。本模块在 **不新增依赖**
的前提下, 用 numpy + 标准库 (``math``) 重写其研究语义:

* **ADF 单位根检验** (:func:`adf_test`): OLS 残差 + MacKinnon 渐近临界值表
  分段插值近似 p-value (非 statsmodels 的 response surface, 仅研究级近似)。
* **Engle-Granger 协整** (:func:`cointegration_test`): 两步 OLS, 对残差做 ADF。
* **GARCH(1,1) 波动率** (:func:`garch_volatility`): 递推条件方差; 参数缺省时用
  **方差目标 + 平方残差一阶自相关** 的稳定矩估计 (非 MLE)。
* **Granger 因果** (:func:`granger_causality`): 逐 lag 比较 restricted /
  unrestricted RSS 的 F 检验, p-value 用正则化不完全 Beta 函数
  (Numerical Recipes 连分式) 解析计算。
* **VIF 多重共线** (:func:`vif_matrix`): 逐列对其余列 OLS 取 R² → 1/(1-R²),
  常数列 / 完美共线 fail-soft (返回 ``None``)。

设计约束 (与 ``attribution.py`` 一致)
-------------------------------------
* **纯研究统计**: 只产出统计量, 不输出任何交易方向 / 仓位 / 订单建议。
* **不新增依赖**: 仅 numpy + 标准库; 不导入 statsmodels / arch / scipy。
* **fail-soft**: 输入为空 / 样本不足 / 设计矩阵秩不足 → 统一返回
  ``status="insufficient_data"`` 且数值字段为 ``None``, **不抛异常**
  (未知 method 仍按编程错误抛 ``ValueError``, 与 ``performance_attribution`` 一致)。
* **非有限值**: 所有输出数值经 :func:`_finite_or_none` 映射, nan/inf → None;
  输入序列中的非有限元素按 SKILL.md 的 ``dropna`` 语义剔除后再检验。
* **p-value 近似**: ADF / 协整使用渐近临界值表插值, Granger 用解析 F 分布
  上侧概率, 均为研究级近似而非生产级数值精度。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "adf_test",
    "cointegration_test",
    "garch_volatility",
    "granger_causality",
    "quant_stats_suite",
    "vif_matrix",
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
    """转 float 一维数组并剔除非有限值 (SKILL.md 的 ``dropna`` 语义); 全空返回 None。"""
    arr = np.asarray(series, dtype=float).ravel()
    arr = arr[np.isfinite(arr)]
    return arr if arr.size > 0 else None


def _ols(design: np.ndarray, target: np.ndarray):
    """普通最小二乘。

    返回 ``(beta, resid, sse, rank, p, sigma2, se_diag, full)``:
    ``se_diag`` 为各系数标准误, 仅在满列秩且自由度 > 0 时有效, 否则为 ``None``;
    ``full`` 标记是否满列秩且 ``n > p``。
    """
    n, p = design.shape
    beta, _resid, rank, _sv = np.linalg.lstsq(design, target, rcond=None)
    resid = target - design @ beta
    sse = float(resid @ resid)
    rank = int(rank) if np.isscalar(rank) else int(np.linalg.matrix_rank(design))
    full = rank == p and n > p
    if full:
        sigma2 = sse / (n - p)
        try:
            xtx_inv = np.linalg.inv(design.T @ design)
        except np.linalg.LinAlgError:
            xtx_inv = np.linalg.pinv(design.T @ design)
        se_diag = np.sqrt(np.maximum(np.diag(xtx_inv) * sigma2, 0.0))
    else:
        sigma2 = None
        se_diag = None
    return beta, resid, sse, rank, p, sigma2, se_diag, full


# ---------------------------------------------------------------------------
# 正则化不完全 Beta + F 分布上侧概率 (Numerical Recipes 连分式)
# ---------------------------------------------------------------------------

_FPMIN = 1e-300
_EPS = 3e-13
_MAXIT = 300


def _betacf(a: float, b: float, x: float) -> float:
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 ``I_x(a, b)``。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _f_sf(f: float, d1: float, d2: float) -> float:
    """F 分布上侧概率 ``P(F > f)`` (survival function)。"""
    if not math.isfinite(f):
        return 0.0 if f > 0 else 1.0
    if f <= 0.0:
        return 1.0
    if d1 <= 0 or d2 <= 0:
        return float("nan")
    # 上侧尾 = I_{d2/(d2+d1·f)}(d2/2, d1/2)
    return _betai(d2 / 2.0, d1 / 2.0, d2 / (d2 + d1 * f))


# ---------------------------------------------------------------------------
# ADF 单位根检验
# ---------------------------------------------------------------------------

# MacKinnon (1994) 渐近临界值 (n→∞), 按 critical_value 升序:
# (critical_value, p_level)。数值取自标准 Dickey-Fuller 渐近分布。
_ADF_CV: dict[str, list[tuple[float, float]]] = {
    "n": [(-2.5658, 0.01), (-1.9393, 0.05), (-1.6156, 0.10)],
    "c": [(-3.4336, 0.01), (-2.8622, 0.05), (-2.5671, 0.10)],
    "ct": [(-3.9589, 0.01), (-3.4106, 0.05), (-3.1273, 0.10)],
    "ctt": [(-4.3714, 0.01), (-3.8321, 0.05), (-3.5534, 0.10)],
}


def _adf_pvalue(tau: float, trend: str) -> float:
    """根据 MacKinnon 渐近临界值对 tau 做分段线性插值 / 外推, 钳到 [0, 1]。"""
    pts = _ADF_CV.get(trend, _ADF_CV["c"])
    cvs = [p[0] for p in pts]
    pvs = [p[1] for p in pts]
    if tau <= cvs[0]:
        slope = (pvs[1] - pvs[0]) / (cvs[1] - cvs[0])
        p = pvs[0] + slope * (tau - cvs[0])
    elif tau >= cvs[-1]:
        slope = (pvs[-1] - pvs[-2]) / (cvs[-1] - cvs[-2])
        p = pvs[-1] + slope * (tau - cvs[-1])
    else:
        p = float(np.interp(tau, cvs, pvs))
    return min(max(p, 0.0), 1.0)


def _adf_fit(y: np.ndarray, lags: int, trend: str):
    """对单组 (lags, trend) 跑一次 ADF 回归。

    模型::

        Δy_t = γ·y_{t-1} + Σ_{i=1}^{p} φ_i·Δy_{t-i} + 确定性项 + ε_t

    返回 ``(tau, sse, n_eff, k_params)`` 或 ``None`` (样本/秩不足)。
    """
    n_total = y.size
    if n_total < 3:
        return None
    p = max(0, int(lags))
    dy = np.diff(y)  # Δy, 长度 n_total-1
    level = y[:-1]  # y_{t-1}, 长度 n_total-1
    start = p
    dep = dy[start:]  # 有效因变量
    m = dep.size
    if m < 2:
        return None
    lvl = level[start : start + m]
    cols = [lvl]
    for i in range(1, p + 1):
        lag = dy[start - i : start - i + m]
        if lag.size != m:
            return None
        cols.append(lag)

    # 确定性项
    det_cols: list[np.ndarray] = []
    if trend in ("c", "ct", "ctt"):
        det_cols.append(np.ones(m))
    if trend in ("ct", "ctt"):
        det_cols.append(np.arange(m, dtype=float))
    if trend == "ctt":
        det_cols.append(np.arange(m, dtype=float) ** 2)

    design = np.column_stack([*det_cols, *cols]) if det_cols else np.column_stack(cols)
    k = design.shape[1]
    if m <= k:
        return None
    beta, _resid, sse, _rank, _p, _sigma2, se_diag, full = _ols(design, dep)
    if not full or se_diag is None:
        return None
    gamma_idx = len(det_cols)  # level 列排在确定性项之后
    se_gamma = float(se_diag[gamma_idx])
    if se_gamma <= 0.0:
        return None
    tau = float(beta[gamma_idx] / se_gamma)
    return tau, sse, m, k


def _select_lag_aic(y: np.ndarray, candidates, trend: str) -> int:
    """在候选 lag 上按 AIC = n·ln(SSE/n) + 2·k 选最小; 全部不可拟合时返回 0。"""
    best: tuple[float, int] | None = None
    for lag in candidates:
        fit = _adf_fit(y, int(lag), trend)
        if fit is None:
            continue
        _tau, sse, n, k = fit
        aic = math.inf if n <= k or sse <= 0 else n * math.log(sse / n) + 2 * k
        if best is None or aic < best[0]:
            best = (aic, int(lag))
    return best[1] if best is not None else 0


def _adf_insufficient(obs: int, lags: int | None = None) -> dict:
    return {
        "status": "insufficient_data",
        "test": "adf",
        "adf_statistic": None,
        "p_value": None,
        "lags_used": int(lags) if lags is not None else None,
        "trend": None,
        "is_stationary": None,
        "observations": int(max(obs, 0)),
        "critical_values": {},
    }


def adf_test(series, lags=None, trend="c", significance=0.05) -> dict:
    """ADF 单位根检验 (H0: 存在单位根 → 非平稳)。

    口径 (SKILL.md "ADF Unit-Root Test")::

        Δy_t = γ·y_{t-1} + Σ φ_i·Δy_{t-i} + 确定性项 + ε_t
        tau = γ̂ / SE(γ̂),   p-value 由 MacKinnon 渐近临界值插值近似

    Args:
        series: 时间序列; 非有限值按 dropna 剔除。
        lags: 增广滞后阶数 p。``None`` (默认) 用 AIC 自动选阶
            (候选 0..min(Schwert 上限, 12)); 显式给出则固定。
        trend: 确定性项 —— ``"c"`` 常数 (默认) / ``"ct"`` 常数+趋势 /
            ``"ctt"`` 常数+趋势+趋势² / ``"n"`` 无。
        significance: 平稳判定显著性水平 (默认 0.05)。

    Returns: ``adf_statistic`` (tau)、``p_value``、``is_stationary``、
        ``lags_used``、``observations``、``critical_values`` (1%/5%/10%)。
        样本 / 秩不足 → ``status="insufficient_data"``。

    研究用途: 结果仅用于统计判断, 不构成任何交易建议。
    """
    y = _clean_series(series)
    if y is None or y.size < 3:
        return _adf_insufficient(0 if y is None else y.size)
    if trend not in _ADF_CV:
        trend = "c"

    if lags is None:
        schwert = round(12.0 * (y.size / 100.0) ** 0.25)
        upper = max(0, min(schwert, (y.size - 1) // 2 - 1, 12))
        chosen = _select_lag_aic(y, range(0, upper + 1), trend)
    else:
        chosen = max(0, int(lags))

    fit = _adf_fit(y, chosen, trend)
    if fit is None:
        return _adf_insufficient(y.size, lags=chosen)
    tau, _sse, n_eff, _k = fit
    p = _adf_pvalue(tau, trend)
    stationary = bool(p < significance)
    cv = {f"{int(pp * 100)}%": _finite_or_none(c) for c, pp in _ADF_CV[trend]}
    return {
        "status": "ok",
        "test": "adf",
        "adf_statistic": _finite_or_none(tau),
        "p_value": _finite_or_none(p),
        "lags_used": int(chosen),
        "trend": trend,
        "is_stationary": stationary,
        "observations": int(n_eff),
        "critical_values": cv,
    }


# ---------------------------------------------------------------------------
# Engle-Granger 协整
# ---------------------------------------------------------------------------


def _coint_insufficient(n: int) -> dict:
    return {
        "status": "insufficient_data",
        "test": "cointegration",
        "hedge_ratio": None,
        "intercept": None,
        "spread_mean": None,
        "spread_std": None,
        "spread": None,
        "adf": None,
        "p_value": None,
        "is_cointegrated": None,
        "observations": int(max(n, 0)),
    }


def cointegration_test(x, y, significance=0.05) -> dict:
    """Engle-Granger 两步协整检验 (H0: 无协整关系)。

    口径 (SKILL.md "Cointegration Test" / "find_hedge_ratio")::

        第一步: y = α + β·x + ε   (OLS, β = hedge_ratio)
        spread = ε̂ = y − α − β·x
        第二步: 对 spread 做 ADF; 平稳 → 协整

    Args:
        x, y: 两条价格序列; 非有限值剔除后按公共长度截齐。
        significance: 协整判定显著性水平。

    Returns: ``hedge_ratio``、``intercept``、``spread`` (残差序列 ndarray)、
        ``spread_mean`` / ``spread_std``、``adf`` (子结果)、``p_value``、
        ``is_cointegrated``、``observations``。

    注: spread 上的 ADF 用标准 ADF 渐近临界值近似; 严格 Engle-Granger 临界值
    略有差异, 此处为研究级近似。仅统计判断, 不构成任何交易建议。
    """
    a = _clean_series(x)
    b = _clean_series(y)
    if a is None or b is None:
        return _coint_insufficient(0)
    n = int(min(a.size, b.size))
    if n < 5:
        return _coint_insufficient(n)
    a, b = a[:n], b[:n]

    design = np.column_stack([np.ones(n), a])
    beta, *_ = np.linalg.lstsq(design, b, rcond=None)
    alpha = float(beta[0])
    hedge = float(beta[1])
    spread = b - alpha - hedge * a

    adf = adf_test(spread, trend="c", significance=significance)
    p = adf.get("p_value")
    is_coint = bool(adf.get("is_stationary")) if adf.get("status") == "ok" else None

    return {
        "status": "ok",
        "test": "cointegration",
        "hedge_ratio": _finite_or_none(hedge),
        "intercept": _finite_or_none(alpha),
        "spread_mean": _finite_or_none(float(np.mean(spread))),
        "spread_std": _finite_or_none(float(np.std(spread, ddof=1))) if spread.size > 1 else None,
        "spread": spread,
        "adf": adf,
        "p_value": p,
        "is_cointegrated": is_coint,
        "observations": n,
    }


# ---------------------------------------------------------------------------
# GARCH(1,1) 波动率
# ---------------------------------------------------------------------------


def _garch_moments(eps: np.ndarray, s2: float) -> tuple[float, float, float]:
    """稳定矩估计 (方差目标 + 平方残差一阶自相关驱动的持续度)。

    口径::

        s2 = mean(ε²)                      # 无条件方差 (方差目标)
        ρ1 = corr(ε²_t, ε²_{t-1})          # 平方残差一阶自相关
        λ = α + β  ← clamp(0.80 + ρ1, 0.5, 0.99)   # 持续度, 有界
        α   ← clamp(0.5·ρ1 + 0.02, 0.005, 0.3)
        β   ← max(λ − α, 0)
        ω   ← max(s2·(1 − λ), 1e-12)       # 方差目标: 长期方差 = ω/(1−λ) ≈ s2

    这是对 GARCH(1,1) 的 **方差目标 (variance targeting)** 简化: 仅用数据方差
    锚定 ω, 用平方残差自相关给出有界的持续度, 牺牲精度换取稳定性 (非 MLE)。
    """
    sq = eps**2
    if sq.size >= 3 and float(np.std(sq)) > 0.0:
        rho1 = float(np.corrcoef(sq[1:], sq[:-1])[0, 1])
    else:
        rho1 = 0.0
    if not math.isfinite(rho1):
        rho1 = 0.0
    lam = min(max(0.80 + rho1, 0.50), 0.99)
    alpha = min(max(0.5 * rho1 + 0.02, 0.005), 0.30)
    beta = max(lam - alpha, 0.0)
    lam = alpha + beta
    omega = max(s2 * (1.0 - lam), 1e-12) if s2 > 0.0 else 1e-12
    return omega, alpha, beta


def _garch_insufficient(obs: int) -> dict:
    return {
        "status": "insufficient_data",
        "test": "garch",
        "omega": None,
        "alpha": None,
        "beta": None,
        "persistence": None,
        "params_source": None,
        "conditional_variance": None,
        "current_volatility": None,
        "long_run_volatility": None,
        "observations": int(max(obs, 0)),
    }


def garch_volatility(returns, omega=None, alpha=None, beta=None) -> dict:
    """GARCH(1,1) 递推条件方差与当前波动率。

    口径 (SKILL.md "GARCH(1,1) Model")::

        ε_t = r_t − μ
        σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1}
        long_run_vol = sqrt(ω / (1 − α − β))

    Args:
        returns: 收益率序列; 非有限值剔除。
        omega / alpha / beta: 模型参数。三者 **同时** 提供且非负、有限时直接
            使用 (``params_source="provided"``); 否则退化到 :func:`_garch_moments`
            稳定矩估计 (``params_source="moment"``)。非法 (负值 / 非有限) 一律
            退化到矩估计, 不抛异常。

    Returns: ``omega`` / ``alpha`` / ``beta`` / ``persistence`` (α+β)、
        ``params_source``、``conditional_variance`` (ndarray)、
        ``current_volatility``、``long_run_volatility``、``observations``。
        条件方差由非负初值与非负参数递推并下限取 0, 保证 **非负**。

    研究用途: 仅刻画波动率结构, 不构成任何交易 / 仓位建议。
    """
    r = _clean_series(returns)
    if r is None or r.size < 3:
        return _garch_insufficient(0 if r is None else r.size)

    eps = r - float(r.mean())
    s2 = float(np.mean(eps**2))

    provided = omega is not None and alpha is not None and beta is not None
    if provided:
        try:
            o_, al_, be_ = float(omega), float(alpha), float(beta)
        except (TypeError, ValueError):
            provided = False
        else:
            if not all(math.isfinite(v) for v in (o_, al_, be_)) or o_ < 0 or al_ < 0 or be_ < 0:
                provided = False

    if provided:
        o, al, be = o_, al_, be_
        source = "provided"
    else:
        o, al, be = _garch_moments(eps, s2)
        source = "moment"

    lam = al + be
    var = np.empty(r.size, dtype=float)
    init = s2 if s2 > 0.0 else (o / (1.0 - lam) if lam < 1.0 else o)
    var[0] = init if (math.isfinite(init) and init >= 0.0) else 0.0
    for t in range(1, r.size):
        v = o + al * (eps[t - 1] ** 2) + be * var[t - 1]
        # 非有限或负 (理论不会发生, 防御性) → 钳到 0, 保证非负方差
        var[t] = v if (math.isfinite(v) and v > 0.0) else 0.0

    current = float(math.sqrt(var[-1])) if var.size else None
    long_run = None
    if lam < 1.0 and o > 0.0 and (1.0 - lam) > 0.0:
        lr = math.sqrt(o / (1.0 - lam))
        long_run = lr if math.isfinite(lr) else None

    return {
        "status": "ok",
        "test": "garch",
        "omega": _finite_or_none(o),
        "alpha": _finite_or_none(al),
        "beta": _finite_or_none(be),
        "persistence": _finite_or_none(lam),
        "params_source": source,
        "conditional_variance": var,
        "current_volatility": _finite_or_none(current),
        "long_run_volatility": _finite_or_none(long_run),
        "observations": int(r.size),
    }


# ---------------------------------------------------------------------------
# Granger 因果
# ---------------------------------------------------------------------------


def _granger_insufficient(n: int) -> dict:
    return {
        "status": "insufficient_data",
        "test": "granger",
        "by_lag": {},
        "best_lag": None,
        "best_p_value": None,
        "any_significant": None,
        "max_lag": None,
        "direction": "x->y",
        "observations": int(max(n, 0)),
    }


def _granger_one_lag(x: np.ndarray, y: np.ndarray, lag: int):
    """单个 lag 的 Granger F 检验。返回 (f_stat, p_value, n_eff) 或 None。"""
    start = lag
    dep = y[start:]
    m = dep.size
    if m < 1:
        return None
    ylags = [y[start - i : start - i + m] for i in range(1, lag + 1)]
    xlags = [x[start - i : start - i + m] for i in range(1, lag + 1)]
    const = np.ones(m)

    # 无约束: const + y 的 lag + x 的 lag
    design_u = np.column_stack([const, *ylags, *xlags])
    k_u = design_u.shape[1]
    if m <= k_u:
        return None
    _b_u, _r_u, sse_u, *_ = _ols(design_u, dep)
    # 约束: const + y 的 lag
    design_r = np.column_stack([const, *ylags])
    _b_r, _r_r, sse_r, *_ = _ols(design_r, dep)

    df_num = lag
    df_den = m - k_u
    if df_den <= 0 or sse_u <= 0.0:
        return None
    # RSS 差可能因数值噪声为微小负值, 钳到 0
    numer = max(sse_r - sse_u, 0.0) / df_num
    denom = sse_u / df_den
    f_stat = numer / denom
    p = _f_sf(f_stat, df_num, df_den)
    if not math.isfinite(p):
        return None
    return float(f_stat), float(p), m


def granger_causality(x, y, max_lag=5, significance=0.05) -> dict:
    """Granger 因果检验: x 是否 (预测性) Granger-cause y。

    口径 (SKILL.md "Granger Causality Test")::

        无约束: y_t = c + Σ a_i·y_{t-i} + Σ b_i·x_{t-i} + ε
        约束:    y_t = c + Σ a_i·y_{t-i}            + e
        F = ((RSS_R − RSS_U)/L) / (RSS_U/(n − k_U)) ~ F(L, n − k_U)

    Args:
        x, y: 两条序列; 非有限值剔除后按公共长度截齐。
        max_lag: 最大检验滞后阶 (默认 5)。
        significance: 单 lag 显著性水平。

    Returns: ``by_lag`` (每个 lag 的 f_statistic / p_value / is_significant /
        observations)、``best_lag``、``best_p_value``、``any_significant``、
        ``direction`` (恒为 ``"x->y"``)、``observations``。

    注: Granger 因果是 **预测性** 因果, 非真实因果; 仅统计判断, 不构成建议。
    """
    a = _clean_series(x)
    b = _clean_series(y)
    if a is None or b is None:
        return _granger_insufficient(0)
    n = int(min(a.size, b.size))
    max_lag = max(1, int(max_lag))
    if n < 4:
        return _granger_insufficient(n)
    a, b = a[:n], b[:n]

    by_lag: dict[int, dict] = {}
    best: tuple[int, float] | None = None
    for lag in range(1, max_lag + 1):
        res = _granger_one_lag(a, b, lag)
        if res is None:
            continue
        f_stat, p, m = res
        by_lag[lag] = {
            "f_statistic": _finite_or_none(f_stat),
            "p_value": _finite_or_none(p),
            "is_significant": bool(p < significance),
            "observations": int(m),
        }
        if best is None or p < best[1]:
            best = (lag, p)

    if not by_lag:
        return _granger_insufficient(n)
    any_sig = any(v["is_significant"] for v in by_lag.values())
    return {
        "status": "ok",
        "test": "granger",
        "by_lag": by_lag,
        "best_lag": best[0] if best else None,
        "best_p_value": _finite_or_none(best[1]) if best else None,
        "any_significant": bool(any_sig),
        "max_lag": int(max_lag),
        "direction": "x->y",
        "observations": n,
    }


# ---------------------------------------------------------------------------
# VIF 多重共线
# ---------------------------------------------------------------------------


def _vif_insufficient(n: int) -> dict:
    return {
        "status": "insufficient_data",
        "test": "vif",
        "vif": {},
        "max_vif": None,
        "observations": int(max(n, 0)),
    }


def vif_matrix(features) -> dict:
    """方差膨胀因子 (VIF) 多重共线诊断。

    口径 (SKILL.md "Multicollinearity Test")::

        对第 j 列: 用其余列 (含常数) OLS 回归, 取 R²_j
        VIF_j = 1 / (1 − R²_j)   (>10 严重共线, >5 需关注)

    Args:
        features: 二维特征矩阵 ``(n, k)`` (一维自动视作单列)。列名按 0..k−1 索引。

    Returns: ``vif`` (各列 VIF, 常数列 / 完美共线 → ``None`` fail-soft)、
        ``max_vif``、``observations``。单列时该列 VIF = 1.0 (无其余解释变量)。

    研究用途: 仅诊断共线结构, 不构成任何交易建议。
    """
    mat = np.asarray(features, dtype=float)
    if mat.ndim == 1:
        mat = mat.reshape(-1, 1)
    if mat.ndim != 2:
        return _vif_insufficient(0)
    n, k = mat.shape
    if n == 0 or k == 0:
        return _vif_insufficient(0)

    out: dict[int, float | None] = {}
    for j in range(k):
        yj = mat[:, j]
        others = [c for c in range(k) if c != j]
        if not others:
            out[j] = _finite_or_none(1.0)
            continue
        design = np.column_stack([np.ones(n), mat[:, others]])
        _beta, _resid, sse, _rank, _p, _sigma2, _se, full = _ols(design, yj)
        ybar = float(yj.mean())
        tss = float(((yj - ybar) ** 2).sum())
        if not full or tss <= 0.0:
            out[j] = None  # 常数列 / 秩不足 → fail-soft
            continue
        r2 = 1.0 - sse / tss
        r2 = min(max(r2, 0.0), 1.0)
        out[j] = None if r2 >= 1.0 else _finite_or_none(1.0 / (1.0 - r2))

    vals = [v for v in out.values() if v is not None]
    return {
        "status": "ok",
        "test": "vif",
        "vif": out,
        "max_vif": _finite_or_none(max(vals)) if vals else None,
        "observations": int(n),
    }


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def quant_stats_suite(
    method: str = "adf",
    series=None,
    x=None,
    y=None,
    returns=None,
    features=None,
    lags=None,
    trend: str = "c",
    max_lag: int = 5,
    omega=None,
    alpha=None,
    beta=None,
    significance: float = 0.05,
) -> dict:
    """统一统计入口: ``method`` 选择底层检验。

    - ``"adf"``: :func:`adf_test` —— 用 ``series / lags / trend``。
    - ``"cointegration"`` (亦接受 ``"coint"``): :func:`cointegration_test` —— 用 ``x / y``。
    - ``"garch"``: :func:`garch_volatility` —— 用 ``returns / omega / alpha / beta``。
    - ``"granger"``: :func:`granger_causality` —— 用 ``x / y / max_lag``。
    - ``"vif"``: :func:`vif_matrix` —— 用 ``features``。

    仅做参数路由, 不引入额外语义; 各项口径见对应 docstring。未知 method 抛
    ``ValueError`` (编程错误, 与 ``performance_attribution`` 一致)。
    """
    if method == "adf":
        return adf_test(series, lags=lags, trend=trend, significance=significance)
    if method in ("cointegration", "coint"):
        return cointegration_test(x, y, significance=significance)
    if method in ("garch", "garch_volatility"):
        return garch_volatility(returns, omega=omega, alpha=alpha, beta=beta)
    if method in ("granger", "granger_causality"):
        return granger_causality(x, y, max_lag=max_lag, significance=significance)
    if method == "vif":
        return vif_matrix(features)
    raise ValueError(f"unknown quant_stats method: {method!r}")
