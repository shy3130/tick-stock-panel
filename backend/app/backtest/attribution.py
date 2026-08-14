"""Performance attribution — Brinson 归因与 Fama-French 多因子分解。

纯计算模块: 只接受 numpy 兼容输入, 无任何网络 / 磁盘 I/O, 不产生交易方向或订单。

口径来源 (并在各函数 docstring 中标注):
    Vibe-Trading `agent/src/skills/performance-attribution/SKILL.md`
        - Brinson-Fachler 单期模型: Allocation / Selection / Interaction 分解
        - Fama-French 多因子: OLS 回归 R_p - R_f = alpha + Σ beta_k·f_k + ε

适配说明 (相对 SKILL.md 的口径调整):
    - SKILL.md 偏展示 (建议 ≥60 样本 / t 统计 / 年化), 本模块只做底层纯计算,
      不做样本量门槛、不做显著性检验、不做年化——上层按需补充。
    - 权重和不等于 1 时内部归一化, 使 Allocation+Selection+Interaction ≡ 超额收益恒等式成立。
    - 所有非有限数值 (nan/inf) 输出为 None, 样本/秩不足统一返回 status="insufficient_data"。
"""
from __future__ import annotations

from collections import OrderedDict

import numpy as np

__all__ = [
    "brinson_attribution",
    "fama_french_attribution",
    "performance_attribution",
]

_WEIGHT_TOL = 1e-9


def _finite_or_none(value: object) -> float | None:
    """非有限值统一映射为 None; 否则返回 float。"""
    if value is None:
        return None
    v = float(value)
    return v if np.isfinite(v) else None


def brinson_attribution(
    portfolio_weights,
    portfolio_returns,
    benchmark_weights,
    benchmark_returns,
    groups=None,
) -> dict:
    """Brinson-Fachler 单期行业 / 分组归因。

    口径 (SKILL.md "Brinson-Fachler Model")::

        Allocation_i  = (w_p,i - w_b,i) · (r_b,i - R_b)
        Selection_i   =  w_b,i            · (r_p,i - r_b,i)
        Interaction_i = (w_p,i - w_b,i)   · (r_p,i - r_b,i)
        total_effect  = Allocation + Selection + Interaction
        R_p / R_b     = Σ w·r (组合 / 基准总收益)

    其中 ``w_p,i`` 等为第 i 组的权重, ``r_p,i`` 为组内加权平均收益, ``R_b`` 为基准总收益。

    Args:
        portfolio_weights / benchmark_weights: 各组 (或各组下属资产) 权重, 一维。
        portfolio_returns / benchmark_returns: 对应收益率, 一维, 长度需与权重一致。
        groups: 行业 / 分组标签; 给定时按标签先在组内聚合
            (权重求和、收益做组内加权平均), 再做 Brinson 分解。
            ``None`` 时每个输入元素自成一组。

    权重和不为 1 时按各自总和归一化, 并在结果中 ``normalized=True``。
    归一化后恒有 ``total_effect == excess_return`` (Brinson-Fachler 代数恒等式)。
    """
    wp = np.asarray(portfolio_weights, dtype=float).ravel()
    rp = np.asarray(portfolio_returns, dtype=float).ravel()
    wb = np.asarray(benchmark_weights, dtype=float).ravel()
    rb = np.asarray(benchmark_returns, dtype=float).ravel()

    n = int(min(wp.size, rp.size, wb.size, rb.size))
    if n == 0:
        return _brinson_empty()

    wp, rp, wb, rb = wp[:n], rp[:n], wb[:n], rb[:n]

    if groups is None:
        labels = [f"group_{i}" for i in range(n)]
    else:
        labels = [str(g) for g in groups]
        if len(labels) < n:
            labels += [f"group_{i}" for i in range(len(labels), n)]
        labels = labels[:n]

    # 组内聚合: 权重求和、收益按 (组内权重) 加权平均
    agg: OrderedDict[str, dict[str, float]] = OrderedDict()
    for i in range(n):
        a = agg.setdefault(labels[i], {"wp": 0.0, "wpr": 0.0, "wb": 0.0, "wbr": 0.0})
        a["wp"] += float(wp[i])
        a["wpr"] += float(wp[i]) * float(rp[i])
        a["wb"] += float(wb[i])
        a["wbr"] += float(wb[i]) * float(rb[i])

    keys = list(agg.keys())
    wp_g = np.array([agg[g]["wp"] for g in keys], dtype=float)
    rp_g = np.array(
        [agg[g]["wpr"] / agg[g]["wp"] if agg[g]["wp"] != 0.0 else 0.0 for g in keys],
        dtype=float,
    )
    wb_g = np.array([agg[g]["wb"] for g in keys], dtype=float)
    rb_g = np.array(
        [agg[g]["wbr"] / agg[g]["wb"] if agg[g]["wb"] != 0.0 else 0.0 for g in keys],
        dtype=float,
    )

    wp_total = float(wp_g.sum())
    wb_total = float(wb_g.sum())
    normalized = not (abs(wp_total - 1.0) <= _WEIGHT_TOL and abs(wb_total - 1.0) <= _WEIGHT_TOL)
    if normalized:
        if abs(wp_total) > _WEIGHT_TOL:
            wp_g = wp_g / wp_total
        if abs(wb_total) > _WEIGHT_TOL:
            wb_g = wb_g / wb_total

    r_b = float((wb_g * rb_g).sum())
    r_p = float((wp_g * rp_g).sum())

    alloc = (wp_g - wb_g) * (rb_g - r_b)
    select = wb_g * (rp_g - rb_g)
    interact = (wp_g - wb_g) * (rp_g - rb_g)

    rows = []
    for i, g in enumerate(keys):
        a, s, it = float(alloc[i]), float(select[i]), float(interact[i])
        rows.append(
            {
                "group": g,
                "portfolio_weight": _finite_or_none(wp_g[i]),
                "benchmark_weight": _finite_or_none(wb_g[i]),
                "portfolio_return": _finite_or_none(rp_g[i]),
                "benchmark_return": _finite_or_none(rb_g[i]),
                "allocation": _finite_or_none(a),
                "selection": _finite_or_none(s),
                "interaction": _finite_or_none(it),
                "total_effect": _finite_or_none(a + s + it),
            }
        )

    total_alloc = float(alloc.sum())
    total_select = float(select.sum())
    total_inter = float(interact.sum())
    return {
        "status": "ok",
        "normalized": normalized,
        "portfolio_return": _finite_or_none(r_p),
        "benchmark_return": _finite_or_none(r_b),
        "excess_return": _finite_or_none(r_p - r_b),
        "groups": rows,
        "allocation": _finite_or_none(total_alloc),
        "selection": _finite_or_none(total_select),
        "interaction": _finite_or_none(total_inter),
        "total_effect": _finite_or_none(total_alloc + total_select + total_inter),
    }


def _brinson_empty() -> dict:
    return {
        "status": "insufficient_data",
        "normalized": False,
        "portfolio_return": None,
        "benchmark_return": None,
        "excess_return": None,
        "groups": [],
        "allocation": None,
        "selection": None,
        "interaction": None,
        "total_effect": None,
    }


def _parse_factors(factor_returns) -> tuple[list[str], list[np.ndarray]]:
    """把 factor_returns 归一成 (因子名列表, 每个因子的一维收益序列)。"""
    if isinstance(factor_returns, dict):
        names = list(factor_returns.keys())
        cols = [np.asarray(factor_returns[k], dtype=float).ravel() for k in names]
        return names, cols
    f = np.asarray(factor_returns, dtype=float)
    if f.ndim == 1:
        return ["factor_1"], [f.ravel()]
    k = int(f.shape[1])
    return [f"factor_{i + 1}" for i in range(k)], [f[:, i].ravel() for i in range(k)]


def fama_french_attribution(portfolio_returns, factor_returns, risk_free=None) -> dict:
    """Fama-French 多因子 OLS 分解。

    口径 (SKILL.md "Multi-Factor Attribution")::

        R_p - R_f = alpha + Σ beta_k · f_k + ε

    OLS 求解 (SKILL.md 建议回归点数 ≥60, 但本模块不做样本门槛, 仅在
    样本不足或设计矩阵秩不足时返回 status="insufficient_data")。

    Args:
        portfolio_returns: 组合收益时间序列, 一维 (长度 T)。
        factor_returns: 因子收益。``dict`` (因子名→序列) 或数组
            (一维=单因子, 二维 ``(T, K)``=多因子, 自动命名为 factor_1..K)。
        risk_free: 无风险利率; 标量或长度 T 的一维序列, ``None`` 视作 0。

    Returns: alpha、各因子 beta / 贡献 (β_k·mean(f_k))、r_squared、
        residual_volatility (回归残差标准误 √(SSE/(T-p)))、observations。
        非有限值输出 None; rank 不足 (如常数因子) → status="insufficient_data"。
    """
    y = np.asarray(portfolio_returns, dtype=float).ravel()
    names, cols = _parse_factors(factor_returns)

    lens = [y.size] + [c.size for c in cols]
    rf_arr: np.ndarray | None = None
    if risk_free is not None:
        rf_arr = np.asarray(risk_free, dtype=float).ravel()
        if rf_arr.size > 1:  # 标量 / 单元素可广播, 不约束公共长度
            lens.append(rf_arr.size)
    t = min(lens)
    if t < 2:
        return _ff_insufficient(t)

    y = y[:t]
    cols = [c[:t] for c in cols]
    if rf_arr is None:
        rf = np.zeros(t)
    elif rf_arr.size == 1:
        rf = np.full(t, float(rf_arr[0]))
    else:
        rf = rf_arr[:t]

    y = y - rf
    k = len(cols)
    x = np.column_stack([np.ones(t), *cols])
    p = k + 1

    beta, _resid, rank, _s = np.linalg.lstsq(x, y, rcond=None)
    if rank < p:
        # 设计矩阵秩不足 (样本不足或因子共线 / 常数因子) → 无法唯一归因
        return _ff_insufficient(t)

    resid = y - x @ beta
    sse = float(resid @ resid)
    ybar = float(y.mean())
    tss = float(((y - ybar) ** 2).sum())
    r_squared = _finite_or_none(1.0 - sse / tss) if tss > 0.0 else None
    residual_vol = _finite_or_none(np.sqrt(sse / (t - p))) if t > p else 0.0

    betas = beta[1:]
    return {
        "status": "ok",
        "alpha": _finite_or_none(beta[0]),
        "betas": {names[j]: _finite_or_none(betas[j]) for j in range(k)},
        "contributions": {
            names[j]: _finite_or_none(betas[j] * float(cols[j].mean())) for j in range(k)
        },
        "r_squared": r_squared,
        "residual_volatility": residual_vol,
        "observations": int(t),
    }


def _ff_insufficient(t: int) -> dict:
    return {
        "status": "insufficient_data",
        "alpha": None,
        "betas": {},
        "contributions": {},
        "r_squared": None,
        "residual_volatility": None,
        "observations": int(max(t, 0)),
    }


def performance_attribution(
    method: str = "brinson",
    portfolio_weights=None,
    portfolio_returns=None,
    benchmark_weights=None,
    benchmark_returns=None,
    groups=None,
    factor_returns=None,
    risk_free=None,
) -> dict:
    """统一归因入口: ``method`` 选择 "brinson" 或 "fama_french"。

    - ``method="brinson"``: 调用 :func:`brinson_attribution`, 使用
      ``portfolio_weights / portfolio_returns / benchmark_weights /
      benchmark_returns / groups``。
    - ``method="fama_french"`` (亦接受 "factor"): 调用 :func:`fama_french_attribution`,
      使用 ``portfolio_returns / factor_returns / risk_free``
      (此处 ``portfolio_returns`` 为组合收益时间序列)。

    本函数仅做参数路由, 不引入额外语义; 两侧口径见各自 docstring。
    """
    if method == "brinson":
        return brinson_attribution(
            portfolio_weights,
            portfolio_returns,
            benchmark_weights,
            benchmark_returns,
            groups,
        )
    if method in ("fama_french", "factor"):
        return fama_french_attribution(portfolio_returns, factor_returns, risk_free)
    raise ValueError(f"unknown attribution method: {method!r}")
