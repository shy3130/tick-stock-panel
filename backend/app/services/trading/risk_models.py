"""风险度量纯函数 — VaR / CVaR / Monte Carlo / 压力测试 / EVT 尾部分析。

来源: 移植自 ``Vibe-Trading/agent/src/skills/risk-analysis/SKILL.md`` 的公式与参数定义,
改为基于 numpy + Python 标准库的纯函数实现 (无 scipy / pandas 依赖),并统一了
"样本不足即拒绝伪造"的语义。

定位与红线:
- 仅供风险透视: 只读本地输入序列,只输出风险度量,不调用任何外部数据源;
- 不生成订单、方向或执行动作;不修改现有 portfolio API 契约;
- VaR/CVaR 一律返回"正数代表损失"的收益率;非有限值统一转 None;
- 样本不足时返回 ``status="insufficient_data"`` 并把度量置 None,绝不伪造数值;
- Monte Carlo 使用确定性 seed;压力测试不修改调用方传入的 scenarios 字典。

实现说明 (源 SKILL 依赖 scipy,本项目不引入该依赖,故作等价纯 numpy 替换):
- 正态分位点 ``norm.ppf`` → Acklam 有理逼近 (与 scipy 误差 < 2e-9);
- GPD 尾部拟合 ``genpareto.fit`` → Hosking-Wallis 概率加权矩 (PWM) 估计器,
  给出标准约定的 shape xi (xi>0 肥尾) 与 scale sigma;
- 偏度 / 超额峰度 → numpy 矩估计公式。

输入约定: 各函数接受 1 维收益序列 (list / tuple / np.ndarray / pd.Series),
代表某组合或标的的日收益率序列。高维输入会被展平为同一收益总体处理。
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

# 风险估计的最小样本量。低于此值认为统计上不可靠,返回 insufficient_data。
# 与 portfolio 风险快照的默认 min_observations (20) 对齐,这里取 30 以给 95% 分位留余量。
_MIN_SAMPLES = 30
# EVT / GPD 拟合所需的最小超限数 (尾部样本数)。
_MIN_EXCEEDANCES = 5
# 年化交易日数,用于把日波动年化。
_TRADING_DAYS = 252

# Acklam 逆正态分布常数 (https://www.wilmott.com, 0.02425 分段阈值)。
_ACKLAM_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
             1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_ACKLAM_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
             6.680131188771972e01, -1.328068155288572e01)
_ACKLAM_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
             -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_ACKLAM_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
             3.754408661907416e00)
_ACKLAM_PLOW = 0.02425


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _clean_returns(returns: Any) -> np.ndarray:
    """把任意收益输入规整为一维 float 数组,丢弃 NaN/Inf。"""
    if returns is None:
        return np.asarray([], dtype=float)
    arr = np.asarray(returns, dtype=float)
    if arr.ndim == 0:
        arr = arr.reshape(1)
    elif arr.ndim > 1:
        arr = arr.ravel()
    return arr[np.isfinite(arr)]


def _finite(value: float) -> float | None:
    """有限 float 则返回原值,否则返回 None。"""
    if value is None:
        return None
    f = float(value)
    return f if np.isfinite(f) else None


def _insufficient(method: str, observations: int, confidence: float, **extra: Any) -> dict[str, Any]:
    """构造"样本不足"结果:不伪造任何度量数值。"""
    result: dict[str, Any] = {
        "method": method,
        "status": "insufficient_data",
        "observations": int(observations),
        "confidence": _finite(confidence),
    }
    result.update(extra)
    return result


def _norm_ppf(p: float) -> float:
    """标准正态分布的分位点函数 (Acklam 有理逼近, 纯 numpy, 与 scipy 误差 < 2e-9)。"""
    if p <= 0.0:
        return float("-inf")
    if p >= 1.0:
        return float("inf")
    a = _ACKLAM_A
    b = _ACKLAM_B
    c = _ACKLAM_C
    d = _ACKLAM_D
    if p < _ACKLAM_PLOW:
        q = np.sqrt(-2.0 * np.log(p))
        return float(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= 1.0 - _ACKLAM_PLOW:
        q = p - 0.5
        r = q * q
        return float((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q) / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = np.sqrt(-2.0 * np.log(1.0 - p))
    return float(-(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])) / \
           ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)


def _skewness(returns: np.ndarray) -> float:
    """样本偏度 (与 scipy.stats.skew 一致的调整偏度, ddof 校正)。"""
    n = returns.size
    if n < 3:
        return 0.0
    m = returns.mean()
    s = returns.std(ddof=1)
    if s == 0.0:
        return 0.0
    z = (returns - m) / s
    g1 = (n / ((n - 1.0) * (n - 2.0))) * np.sum(z ** 3)
    return float(g1)


def _excess_kurtosis(returns: np.ndarray) -> float:
    """样本超额峰度 (Fisher 形式, 与 scipy.stats.kurtosis(fisher=True) 一致)。"""
    n = returns.size
    if n < 4:
        return 0.0
    m = returns.mean()
    s = returns.std(ddof=1)
    if s == 0.0:
        return 0.0
    z = (returns - m) / s
    num = n * (n + 1.0) * np.sum(z ** 4)
    den = (n - 1.0) * (n - 2.0) * (n - 3.0)
    g2 = num / den - 3.0 * (n - 1.0) ** 2 / ((n - 2.0) * (n - 3.0))
    return float(g2)


def _gpd_pwm(exceedances: np.ndarray) -> tuple[float, float] | tuple[None, None]:
    """广义帕累托分布的 Hosking-Wallis 概率加权矩估计 (纯 numpy)。

    返回标准约定的 (shape xi, scale sigma): xi>0 表示肥尾。
    拟合失败或 scale 非正时返回 (None, None)。
    """
    x = np.sort(np.asarray(exceedances, dtype=float))
    n = x.size
    if n < 2 or not np.all(x > 0):
        return None, None
    f_hat = (np.arange(1, n + 1) - 0.35) / n
    b0 = float(np.mean(x))
    b1 = float(np.mean(x * (1.0 - f_hat)))
    denom = b0 - 2.0 * b1
    if not np.isfinite(denom) or abs(denom) < 1e-300:
        return None, None
    k = b0 / denom - 2.0          # Hosking k (与 scipy 的 xi 反号)
    alpha = 2.0 * b0 * b1 / denom  # scale sigma
    xi = -k                       # 标准约定: xi>0 肥尾
    sigma = alpha
    if not np.isfinite(xi) or not np.isfinite(sigma) or sigma <= 0:
        return None, None
    return float(xi), float(sigma)


def _descriptive(returns: np.ndarray) -> dict[str, float | None]:
    """收益序列的描述性统计 (非有限值转 None)。"""
    mu = float(np.mean(returns)) if returns.size else 0.0
    sigma = float(np.std(returns, ddof=1)) if returns.size > 1 else 0.0
    return {
        "mean": _finite(mu),
        "std": _finite(sigma),
        "annualizedVolatility": _finite(float(np.sqrt(sigma ** 2 * _TRADING_DAYS))),
        "skewness": _finite(_skewness(returns)) if returns.size > 2 else None,
        "excessKurtosis": _finite(_excess_kurtosis(returns)) if returns.size > 3 else None,
        "min": _finite(float(np.min(returns))) if returns.size else None,
        "max": _finite(float(np.max(returns))) if returns.size else None,
    }


# --------------------------------------------------------------------------- #
# 1. 历史模拟 VaR
# --------------------------------------------------------------------------- #
def historical_var(
    returns: Sequence[float] | np.ndarray,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """历史模拟法 VaR。

    公式 (移植自源 SKILL.md):
        sorted_returns 升序排列; index = int((1 - confidence) * n);
        var_1d = -sorted_returns[index]; 正数代表损失。
    """
    arr = _clean_returns(returns)
    n = arr.size
    if n < _MIN_SAMPLES:
        return _insufficient("historical_var", n, confidence, var=None)
    sorted_returns = np.sort(arr)
    index = max(0, min(int((1.0 - confidence) * n), n - 1))
    var = -float(sorted_returns[index])
    return {
        "method": "historical",
        "status": "ok",
        "confidence": _finite(confidence),
        "observations": int(n),
        "var": _finite(var),
    }


# --------------------------------------------------------------------------- #
# 2. 历史模拟 CVaR / Expected Shortfall
# --------------------------------------------------------------------------- #
def historical_cvar(
    returns: Sequence[float] | np.ndarray,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """历史模拟法 CVaR: 超过 VaR 阈值的所有尾部损失的均值。

    公式 (移植自源 SKILL.md):
        var = historical_var(returns, confidence);
        tail_losses = returns[returns < -var];
        cvar = -mean(tail_losses) if any else var。
    """
    arr = _clean_returns(returns)
    n = arr.size
    if n < _MIN_SAMPLES:
        return _insufficient("historical_cvar", n, confidence, var=None, cvar=None)
    hv = historical_var(arr, confidence)
    threshold = -float(hv["var"])  # 还原为负的收益率阈值 (var 本身为正损失)
    tail_losses = arr[arr < threshold]
    if tail_losses.size > 0:
        cvar = -float(np.mean(tail_losses))
    else:
        cvar = float(hv["var"])
    return {
        "method": "historical_cvar",
        "status": "ok",
        "confidence": _finite(confidence),
        "observations": int(n),
        "var": hv["var"],
        "cvar": _finite(cvar),
    }


# --------------------------------------------------------------------------- #
# 3. 参数 (正态) VaR
# --------------------------------------------------------------------------- #
def parametric_var(
    returns: Sequence[float] | np.ndarray,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """参数法 (正态假设) VaR。

    公式 (移植自源 SKILL.md):
        mu = mean(returns); sigma = std(returns);
        z = norm.ppf(1 - confidence);
        var_1d = -(mu + z * sigma); 正数代表损失。
    """
    arr = _clean_returns(returns)
    n = arr.size
    if n < _MIN_SAMPLES:
        return _insufficient("parametric_var", n, confidence, var=None)
    mu = float(np.mean(arr))
    sigma = float(np.std(arr, ddof=1))
    z = _norm_ppf(1.0 - confidence)
    var = -(mu + z * sigma)
    return {
        "method": "parametric",
        "status": "ok",
        "confidence": _finite(confidence),
        "observations": int(n),
        "mean": _finite(mu),
        "std": _finite(sigma),
        "var": _finite(var),
    }


# --------------------------------------------------------------------------- #
# 4. Monte Carlo VaR (单期, 正态校准, 确定性 seed)
# --------------------------------------------------------------------------- #
def monte_carlo_var(
    returns: Sequence[float] | np.ndarray,
    confidence: float = 0.95,
    simulations: int = 10000,
    seed: int = 42,
) -> dict[str, Any]:
    """Monte Carlo VaR。

    用样本均值/标准差校准正态分布,生成 ``simulations`` 条单期 (1 日) 模拟收益,
    再取分位得到 VaR/CVaR。使用 ``np.random.default_rng(seed)`` 保证确定性可复现。
    正数代表损失。
    """
    arr = _clean_returns(returns)
    n = arr.size
    sims = max(int(simulations), 1)
    if n < _MIN_SAMPLES:
        return _insufficient(
            "monte_carlo_var", n, confidence,
            var=None, cvar=None, simulations=sims, seed=int(seed),
        )
    mu = float(np.mean(arr))
    sigma = float(np.std(arr, ddof=1))
    rng = np.random.default_rng(int(seed))
    simulated = mu + sigma * rng.standard_normal(sims)
    var = -float(np.percentile(simulated, (1.0 - confidence) * 100.0))
    threshold = -var  # 负的收益率阈值
    tail = simulated[simulated < threshold]
    cvar = -float(np.mean(tail)) if tail.size > 0 else var
    return {
        "method": "monte_carlo",
        "status": "ok",
        "confidence": _finite(confidence),
        "observations": int(n),
        "simulations": sims,
        "seed": int(seed),
        "mean": _finite(mu),
        "std": _finite(sigma),
        "var": _finite(var),
        "cvar": _finite(cvar),
        "probLoss": _finite(float(np.mean(simulated < 0.0))),
    }


# --------------------------------------------------------------------------- #
# 5. 压力测试
# --------------------------------------------------------------------------- #
def stress_test(
    returns: Sequence[float] | np.ndarray,
    scenarios: dict[str, float],
) -> dict[str, Any]:
    """压力测试: 对每个情景冲击评估组合损失,并与历史 95% VaR 基准对比。

    - ``scenarios``: ``{情景名: 冲击收益率}``,负值代表下跌;
    - 组合损失 = -冲击 (正数代表损失金额/收益率);
    - 不修改调用方传入的 ``scenarios`` (内部仅读取副本)。
    样本不足时无法计算 VaR 基准,但情景损失仍如实返回,``breachesVar95`` 为 None。
    """
    arr = _clean_returns(returns)
    n = arr.size
    baseline = historical_var(arr, 0.95)
    var95 = baseline["var"] if baseline["status"] == "ok" else None

    # 仅读取,绝不改写调用方字典。
    result_scenarios: dict[str, dict[str, Any]] = {}
    for name, shock in dict(scenarios).items():
        try:
            shock_f = float(shock)
        except (TypeError, ValueError):
            shock_f = float("nan")
        shock_val = _finite(shock_f)
        loss = _finite(-shock_f) if shock_val is not None else None
        breaches: bool | None = None
        if loss is not None and var95 is not None:
            breaches = bool(loss > var95)
        result_scenarios[str(name)] = {
            "shock": shock_val,
            "loss": loss,
            "breachesVar95": breaches,
        }

    worst_name: str | None = None
    worst_loss: float | None = None
    for name, item in result_scenarios.items():
        if item["loss"] is not None and (worst_loss is None or item["loss"] > worst_loss):
            worst_loss = item["loss"]
            worst_name = name

    return {
        "method": "stress_test",
        "status": baseline["status"],
        "observations": int(n),
        "var95": var95,
        "scenarioCount": len(result_scenarios),
        "scenarios": result_scenarios,
        "worstScenario": worst_name,
        "worstLoss": worst_loss,
    }


# --------------------------------------------------------------------------- #
# 6. EVT 尾部分析 (POT / 广义帕累托分布)
# --------------------------------------------------------------------------- #
def evt_tail_summary(
    returns: Sequence[float] | np.ndarray,
    threshold_quantile: float = 0.95,
) -> dict[str, Any]:
    """极值理论尾部分析 (Peaks Over Threshold)。

    移植自源 SKILL.md 的 ``fit_gpd_tail``:
        threshold = percentile(returns, (1 - threshold_quantile) * 100);
        exceedances = threshold - returns[returns < threshold];  (取正)
        对超限拟合广义帕累托分布 (本项目用 Hosking-Wallis PWM 代替 scipy MLE)。
    ``threshold_quantile`` 语义与置信度一致 (0.95 => 考察最差 5% 尾部)。
    同时输出偏度/超额峰度/尾部比率等尾部风险指标。
    """
    arr = _clean_returns(returns)
    n = arr.size
    if n < _MIN_SAMPLES:
        return _insufficient(
            "evt_tail", n, threshold_quantile,
            threshold=None, nExceedances=0, shapeXi=None, scaleSigma=None,
            tailType=None, skewness=None, excessKurtosis=None, tailRatio=None,
        )
    tail_pct = (1.0 - threshold_quantile) * 100.0
    threshold = float(np.percentile(arr, tail_pct))
    exceedances = threshold - arr[arr < threshold]
    n_exc = int(exceedances.size)

    skewness = _finite(_skewness(arr))
    excess_kurt = _finite(_excess_kurtosis(arr))
    p_lo = float(np.percentile(arr, 5.0))
    p_hi = float(np.percentile(arr, 95.0))
    tail_ratio = _finite(abs(p_lo) / abs(p_hi)) if abs(p_hi) > 0 else None

    if n_exc < _MIN_EXCEEDANCES:
        return {
            "method": "evt_tail",
            "status": "insufficient_data",
            "thresholdQuantile": _finite(threshold_quantile),
            "observations": int(n),
            "threshold": _finite(threshold),
            "nExceedances": n_exc,
            "shapeXi": None,
            "scaleSigma": None,
            "tailType": None,
            "skewness": skewness,
            "excessKurtosis": excess_kurt,
            "tailRatio": tail_ratio,
        }

    xi, sigma = _gpd_pwm(exceedances)
    if xi is None or sigma is None:
        shape_out: float | None = None
        scale_out: float | None = None
        tail_type: str | None = None
    else:
        shape_out = _finite(xi)
        scale_out = _finite(sigma)
        if xi > 0:
            tail_type = "fat tail (dangerous)"
        elif xi == 0:
            tail_type = "exponential tail"
        else:
            tail_type = "thin tail (bounded)"

    return {
        "method": "evt_tail",
        "status": "ok" if shape_out is not None else "insufficient_data",
        "thresholdQuantile": _finite(threshold_quantile),
        "observations": int(n),
        "threshold": _finite(threshold),
        "nExceedances": n_exc,
        "shapeXi": shape_out,
        "scaleSigma": scale_out,
        "tailType": tail_type,
        "skewness": skewness,
        "excessKurtosis": excess_kurt,
        "tailRatio": tail_ratio,
    }


# --------------------------------------------------------------------------- #
# 7. 组合风险模型汇总
# --------------------------------------------------------------------------- #
def portfolio_risk_models(
    returns: Sequence[float] | np.ndarray,
    confidence: float = 0.95,
    simulations: int = 10000,
    seed: int = 42,
) -> dict[str, Any]:
    """一键汇总: 历史VaR/CVaR + 参数VaR + Monte Carlo + EVT 尾部分析 + 描述统计。

    样本不足时整体返回 ``insufficient_data``,所有度量置 None,不伪造数值。
    """
    arr = _clean_returns(returns)
    n = arr.size
    base: dict[str, Any] = {
        "source": "vibe-trading/agent risk-analysis skill (ported; risk-perspective only)",
        "confidence": _finite(confidence),
        "observations": int(n),
        "minSamples": _MIN_SAMPLES,
    }
    if n < _MIN_SAMPLES:
        empty_desc = {
            "mean": None, "std": None, "annualizedVolatility": None,
            "skewness": None, "excessKurtosis": None, "min": None, "max": None,
        }
        return {
            **base,
            "status": "insufficient_data",
            "descriptive": _descriptive(arr) if n else empty_desc,
            "historicalVar": None,
            "historicalCvar": None,
            "parametricVar": None,
            "monteCarlo": None,
            "evt": None,
        }

    hv = historical_var(arr, confidence)
    cv = historical_cvar(arr, confidence)
    pv = parametric_var(arr, confidence)
    mc = monte_carlo_var(arr, confidence, simulations, seed)
    evt = evt_tail_summary(arr, confidence)
    return {
        **base,
        "status": "ok",
        "descriptive": _descriptive(arr),
        "historicalVar": hv["var"],
        "historicalCvar": cv["cvar"],
        "parametricVar": pv["var"],
        "monteCarlo": mc,
        "evt": evt,
    }
