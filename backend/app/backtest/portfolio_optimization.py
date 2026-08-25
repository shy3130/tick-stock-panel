"""组合优化器 — 四种经典权重优化方法（纯 numpy 诊断型实现）。

移植来源
--------
``../Vibe-Trading/agent/src/skills/asset-allocation/SKILL.md``

上游技能文档描述了资产配置理论（MPT / Risk Budgeting / Risk Parity）与四类
内置优化器（equal_volatility / risk_parity / mean_variance /
max_diversification）。本模块将其核心方法论适配为纯 numpy 实现的**诊断型**
优化器：只读取收益矩阵和约束，输出权重和完整诊断信息，不写账户、不生成订单。

与已有 ``app/backtest/optimizers.py`` 的区别
---------------------------------------------
``optimizers.portfolio_weights`` 返回裸权重数组，供回测引擎内部快速调用，
迭代次数固定、无诊断输出、无显式收敛判定。
本模块面向**组合分析与诊断**：返回结构化 ``OptimizationResult``，包含
status / converged / iterations / warnings 等完整元信息，并支持显式的
long-only + min/max 权重约束与收敛容差控制。

四种方法
--------
1. ``equal_weight`` — 等权 1/N，无迭代。
2. ``minimum_variance`` — 协方差正则化 + long-only simplex 投影的投影梯度
   下降，最小化 ``w'Σw``（Markowitz 最小方差）。
3. ``maximum_sharpe`` — 给定无风险收益率 ``risk_free_rate`` 下的长仓约束
   最大 Sharpe（切线组合），投影梯度上升 + 回溯线搜索。
4. ``risk_parity`` — 循环坐标下降（CCD）迭代求 equal risk contribution，
   带最大迭代次数 ``max_iter`` 与收敛容差 ``tol``。

约束
----
默认 long-only + simplex（``w ≥ 0, Σw = 1``）。可通过 ``min_weight`` /
``max_weight`` 进一步收紧为盒约束 ``min_weight ≤ w_i ≤ max_weight``。
当盒约束不可行（如 ``min_weight × N > 1``）时回退到等权并产生 warning。

全部计算仅依赖 numpy，不引入 scipy / cvxpy。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# 公共接口
# ---------------------------------------------------------------------------

VALID_METHODS = ("equal_weight", "minimum_variance", "maximum_sharpe", "risk_parity")

#: 至少需要这么多期有效收益才能做协方差估计；不足则降级为等权。
_MIN_SAMPLES = 2


@dataclass
class OptimizationResult:
    """组合优化统一诊断结果。

    所有收益/风险指标基于**输入收益矩阵的原始频率**计算（日频输入 → 日频指标），
    不做年化。调用方按需自行年化（如 ``× sqrt(252)``）。

    Attributes
    ----------
    status : str
        ``"ok"`` 正常收敛；``"degraded"`` 因数值问题降级（如奇异协方差）但仍有输出；
        ``"insufficient_data"`` 样本不足，返回等权；``"invalid_input"`` 输入非法
        （空矩阵、未知方法等），weights 为空。
    weights : np.ndarray
        长度为 N 的权重向量，满足约束。
    expected_return : float
        组合期望收益 ``w'μ``（μ 为样本均值）。
    volatility : float
        组合波动率 ``sqrt(w'Σw)``。
    sharpe : float
        ``(expected_return - risk_free_rate) / volatility``；volatility ≈ 0 时为 0。
    converged : bool
        迭代型方法是否在 ``max_iter`` 内收敛；``equal_weight`` 恒为 True。
    iterations : int
        实际迭代次数；``equal_weight`` 为 0。
    warnings : list[str]
        降级 / 边界 / 数值警告信息。
    method : str
        实际使用的方法名。
    """

    status: str
    weights: np.ndarray
    expected_return: float
    volatility: float
    sharpe: float
    converged: bool
    iterations: int
    warnings: list[str] = field(default_factory=list)
    method: str = ""


def optimize_portfolio(
    returns: np.ndarray,
    method: str,
    *,
    risk_free_rate: float = 0.0,
    min_weight: float = 0.0,
    max_weight: float = 1.0,
    max_iter: int = 1000,
    tol: float = 1e-8,
    cov_regularization: float = 1e-8,
) -> OptimizationResult:
    """统一组合优化入口。

    Parameters
    ----------
    returns : np.ndarray
        收益矩阵，形状 ``(T, N)`` — T 期、N 个资产。允许含 NaN/Inf 行（自动剔除）。
    method : str
        ``"equal_weight"`` / ``"minimum_variance"`` / ``"maximum_sharpe"`` /
        ``"risk_parity"``。
    risk_free_rate : float
        无风险收益率（与输入同频率），用于 ``maximum_sharpe`` 优化及所有方法的
        Sharpe 报告。默认 0。
    min_weight : float
        单资产权重下界，默认 0（long-only）。
    max_weight : float
        单资产权重上界，默认 1（允许全仓单资产）。
    max_iter : int
        迭代型方法的最大迭代次数。
    tol : float
        收敛容差：迭代型方法当 ``‖w_new − w_old‖ < tol`` 时判定收敛。
    cov_regularization : float
        协方差对角正则化项 ``Σ + λI``，默认 1e-8，防止奇异。

    Returns
    -------
    OptimizationResult
    """
    warnings: list[str] = []

    # -- 方法校验 ----------------------------------------------------------
    if method not in VALID_METHODS:
        return _empty_result(method, [f"未知方法 '{method}'；可选: {VALID_METHODS}"])

    # -- 输入清洗 ----------------------------------------------------------
    r = np.asarray(returns, dtype=float)
    if r.ndim != 2 or r.size == 0:
        return _empty_result(method, ["收益矩阵为空或非二维"])

    n = r.shape[1]
    if n == 0:
        return _empty_result(method, ["资产数为 0"])

    # 单资产：唯一可行权重为 1.0（若边界允许）
    if n == 1:
        w = np.array([1.0])
        if min_weight > 1.0 + 1e-12 or max_weight < 1.0 - 1e-12:
            warnings.append("单资产下 min/max_weight 约束不可行；使用权重 1.0")
        return _build_result("ok", w, r, risk_free_rate, converged=True,
                             iterations=0, warnings=warnings, method=method)

    # 过滤 NaN/Inf 行
    clean = r[np.isfinite(r).all(axis=1)]
    t = clean.shape[0]

    # -- equal_weight 不需要协方差 -----------------------------------------
    if method == "equal_weight":
        w = np.full(n, 1.0 / n)
        status = "ok"
        if w.min() < min_weight - 1e-12:
            warnings.append(
                f"等权 1/{n}={1/n:.4f} 低于 min_weight={min_weight}；"
                "约束不可行，仍返回等权"
            )
            status = "degraded"
        if w.max() > max_weight + 1e-12:
            warnings.append(
                f"等权 1/{n}={1/n:.4f} 高于 max_weight={max_weight}；"
                "约束不可行，仍返回等权"
            )
            status = "degraded"
        # 用清洗后的数据计算诊断指标（样本不足时用原始）
        diag_r = clean if t >= 1 else r
        return _build_result(status, w, diag_r, risk_free_rate,
                             converged=True, iterations=0,
                             warnings=warnings, method=method)

    # -- 样本不足检查 ------------------------------------------------------
    if t < _MIN_SAMPLES:
        w = np.full(n, 1.0 / n)
        warnings.append(
            f"有效样本 {t} < {_MIN_SAMPLES}，无法估计协方差；降级为等权"
        )
        return _build_result("insufficient_data", w, clean if t >= 1 else r,
                             risk_free_rate, converged=False, iterations=0,
                             warnings=warnings, method=method)

    # -- 协方差与均值 ------------------------------------------------------
    mu = clean.mean(axis=0)
    cov = np.atleast_2d(np.cov(clean, rowvar=False))
    if cov.shape != (n, n):
        # np.cov 对某些退化输入可能返回标量
        cov = np.eye(n) * float(np.var(clean[:, 0])) if n == 1 else np.eye(n)

    cov += np.eye(n) * cov_regularization

    # 恒定收益检测（方差 ≈ 0）
    diag_var = np.maximum(np.diag(cov), 0.0)
    if np.all(diag_var <= cov_regularization * 10):
        warnings.append(
            "所有资产方差接近 0（恒定收益）；协方差已正则化，"
            "min_variance / risk_parity 退化为等权"
        )

    # -- 约束可行性检查 ----------------------------------------------------
    feasible, fb_warnings = _check_bounds_feasible(n, min_weight, max_weight)
    warnings.extend(fb_warnings)

    # -- 分派到具体优化器 --------------------------------------------------
    if method == "minimum_variance":
        w, converged, iters = _minimum_variance(
            cov, n, min_weight, max_weight, max_iter, tol, warnings, feasible
        )
    elif method == "maximum_sharpe":
        w, converged, iters = _maximum_sharpe(
            mu, cov, risk_free_rate, n, min_weight, max_weight,
            max_iter, tol, warnings, feasible
        )
    elif method == "risk_parity":
        w, converged, iters = _risk_parity(
            cov, n, min_weight, max_weight, max_iter, tol, warnings, feasible
        )
    else:  # 不可达（已在上面校验）
        raise AssertionError(f"unreachable: {method}")

    # 确保权重精确满足 simplex（投影后浮点误差累积）
    w = _snap_to_simplex(w, min_weight, max_weight, n)

    status = _derive_status(warnings)
    return _build_result(status, w, clean, risk_free_rate,
                         converged=converged, iterations=iters,
                         warnings=warnings, method=method)


# ---------------------------------------------------------------------------
# 优化器实现
# ---------------------------------------------------------------------------

def _minimum_variance(
    cov: np.ndarray,
    n: int,
    lo: float,
    hi: float,
    max_iter: int,
    tol: float,
    warnings: list[str],
    feasible: bool,
) -> tuple[np.ndarray, bool, int]:
    """投影梯度下降最小化 ``w'Σw``。

    目标 ``f(w) = w'Σw`` 为凸二次型，梯度 ``∇f = 2Σw``，Hessian ``2Σ``。
    Lipschitz 常数 ``L = 2λ_max(Σ)``，步长 ``α = 1/L`` 保证单调下降。
    """
    if not feasible:
        return np.full(n, 1.0 / n), False, 0

    # 初始点：无约束最小方差投影到 simplex
    try:
        inv_cov_1 = np.linalg.solve(cov, np.ones(n))
        if np.all(np.isfinite(inv_cov_1)) and inv_cov_1.sum() != 0:
            w0 = inv_cov_1 / inv_cov_1.sum()
            w0 = np.maximum(w0, 0.0)
            s = w0.sum()
            w0 = w0 / s if s > 0 else np.full(n, 1.0 / n)
        else:
            w0 = np.full(n, 1.0 / n)
    except np.linalg.LinAlgError:
        w0 = np.full(n, 1.0 / n)

    w = _project(w0, lo, hi, n)

    # 谱步长
    lam_max = float(np.linalg.eigvalsh(cov)[-1])
    if lam_max <= 0:
        warnings.append("协方差最大特征值 ≤ 0；退化为等权")
        return np.full(n, 1.0 / n), False, 0
    step = 1.0 / (2.0 * lam_max)

    converged = False
    for k in range(max_iter):
        grad = 2.0 * (cov @ w)
        w_new = _project(w - step * grad, lo, hi, n)
        diff = float(np.linalg.norm(w_new - w))
        w = w_new
        if diff < tol:
            converged = True
            break

    if not converged:
        warnings.append(f"minimum_variance 在 {max_iter} 次迭代内未收敛（最后步长 {diff:.2e}）")

    return w, converged, k + 1


def _maximum_sharpe(
    mu: np.ndarray,
    cov: np.ndarray,
    rf: float,
    n: int,
    lo: float,
    hi: float,
    max_iter: int,
    tol: float,
    warnings: list[str],
    feasible: bool,
) -> tuple[np.ndarray, bool, int]:
    """投影梯度上升最大化 Sharpe ``(μ'w − rf) / √(w'Σw)``，长仓约束。

    使用归一化梯度 + 回溯线搜索保证 Sharpe 单调递增。初始点取无约束
    切线组合的正部投影。收敛判据：投影梯度范数 ``‖Π(w+∇S) − w‖ < tol``
    **或** Sharpe 增量 ``|ΔS| < tol``（取先满足者）。
    """
    if not feasible:
        return np.full(n, 1.0 / n), False, 0

    excess = mu - rf
    if np.all(excess <= 0):
        warnings.append(
            "所有资产超额收益 ≤ 0；不存在正 Sharpe 切线组合，退化为最小方差"
        )
        return _minimum_variance(cov, n, lo, hi, max_iter, tol, warnings, feasible)

    # 初始点：无约束切线组合 w ∝ Σ⁻¹ e，取正部后归一化
    try:
        tangency = np.linalg.solve(cov, excess)
        if np.all(np.isfinite(tangency)):
            tangency = np.maximum(tangency, 0.0)
            s = tangency.sum()
            w0 = tangency / s if s > 0 else np.full(n, 1.0 / n)
        else:
            w0 = np.full(n, 1.0 / n)
    except np.linalg.LinAlgError:
        w0 = np.full(n, 1.0 / n)

    w = _project(w0, lo, hi, n)

    lam_max = float(np.linalg.eigvalsh(cov)[-1])
    if lam_max <= 0:
        warnings.append("协方差最大特征值 ≤ 0；退化为等权")
        return np.full(n, 1.0 / n), False, 0

    converged = False
    diff = 0.0
    for k in range(max_iter):
        sw = cov @ w
        var = float(w @ sw)
        if var <= 0:
            warnings.append("组合方差 ≤ 0（退化）；终止迭代")
            break
        sigma = np.sqrt(var)
        er = float(excess @ w)
        sharpe_val = er / sigma

        # Sharpe 梯度: ∇S = (e − (er/var)·Σw) / σ
        grad = (excess - (er / var) * sw) / sigma

        # 投影梯度收敛判据（约束优化的标准驻点条件）
        pg = _project(w + grad, lo, hi, n) - w
        if float(np.linalg.norm(pg)) < tol:
            converged = True
            break

        # 归一化方向 + 回溯线搜索
        grad_norm = float(np.linalg.norm(grad))
        if grad_norm < 1e-15:
            converged = True
            break
        direction = grad / grad_norm
        step = 1.0  # 归一化方向下步长直接对应权重移动量
        improved = False
        w_new = w
        for _bt in range(50):
            w_trial = _project(w + step * direction, lo, hi, n)
            sw_t = cov @ w_trial
            var_t = max(float(w_trial @ sw_t), 1e-30)
            sigma_t = np.sqrt(var_t)
            er_t = float(excess @ w_trial)
            sharpe_t = er_t / sigma_t
            if sharpe_t >= sharpe_val - 1e-15:
                w_new = w_trial
                improved = True
                break
            step *= 0.5

        if not improved:
            # 无法沿梯度方向改进 → 已达 KKT 驻点
            converged = True
            break

        diff = float(np.linalg.norm(w_new - w))
        # Sharpe 增量收敛判据
        sharpe_gain = _sharpe_at(w_new, excess, cov) - sharpe_val
        w = w_new
        if diff < tol or abs(sharpe_gain) < tol:
            converged = True
            break

    if not converged:
        warnings.append(
            f"maximum_sharpe 在 {max_iter} 次迭代内未收敛（最后步长 {diff:.2e}）"
        )

    return w, converged, k + 1


def _sharpe_at(w: np.ndarray, excess: np.ndarray, cov: np.ndarray) -> float:
    """计算组合 Sharpe 比率的辅助函数."""
    var = float(w @ cov @ w)
    if var <= 0:
        return 0.0
    return float(excess @ w) / np.sqrt(var)


def _risk_parity(
    cov: np.ndarray,
    n: int,
    lo: float,
    hi: float,
    max_iter: int,
    tol: float,
    warnings: list[str],
    feasible: bool,
) -> tuple[np.ndarray, bool, int]:
    """循环坐标下降（CCD）求 equal risk contribution。

    采用 Chaves-Denison-Gestel (2012) / Griveau-Billion et al. (2013) 的
    迭代格式：每轮固定当前组合方差 ``σ²(p) = w'Σw`` 作为 Lagrange 乘子，
    对每个坐标 i 解二次 ``α_i w_i² + β_i w_i − b_i·σ²(p) = 0`` 取正根。
    此格式**尺度不变**——归一化不破坏逐坐标条件，因而 CCD 在单纯形上稳定收敛。

    目标等价于 ``RC_i / ΣRC = b_i = 1/N``（equal risk contribution）。
    收敛判定：归一化风险贡献与目标 ``b_i`` 的最大偏差 < tol。
    """
    if not feasible:
        return np.full(n, 1.0 / n), False, 0

    budgets = np.full(n, 1.0 / n)
    w = np.full(n, 1.0 / n)

    converged = False
    max_dev = float("inf")
    for k in range(max_iter):
        sw = cov @ w
        port_var = float(w @ sw)
        if port_var <= 0:
            warnings.append("risk_parity 组合方差 ≤ 0；退化为等权")
            return np.full(n, 1.0 / n), False, k + 1
        for i in range(n):
            alpha = float(cov[i, i])
            if alpha <= 0:
                continue
            beta = float(sw[i] - cov[i, i] * w[i])
            disc = beta * beta + 4.0 * alpha * budgets[i] * port_var
            w_i_new = (-beta + np.sqrt(max(disc, 0.0))) / (2.0 * alpha)
            # 增量更新 sw（O(n) 而非 O(n²)）
            delta = w_i_new - w[i]
            if delta != 0.0:
                sw += cov[:, i] * delta
                w[i] = w_i_new

        # 归一化到 simplex（尺度不变格式下不破坏逐坐标条件）
        s = w.sum()
        if s <= 0 or not np.isfinite(s):
            warnings.append("risk_parity 迭代产生非正/非有限权重；退化为等权")
            return np.full(n, 1.0 / n), False, k + 1
        w = w / s

        # 收敛判定：归一化风险贡献
        rc = w * (cov @ w)
        rc_total = rc.sum()
        if rc_total <= 0:
            warnings.append("risk_parity 风险贡献总和 ≤ 0；退化为等权")
            return np.full(n, 1.0 / n), False, k + 1
        rc_norm = rc / rc_total
        max_dev = float(np.max(np.abs(rc_norm - budgets)))
        if max_dev < tol:
            converged = True
            break

    if not converged:
        warnings.append(
            f"risk_parity 在 {max_iter} 次迭代内未收敛（最后风险贡献偏差 {max_dev:.2e}）"
        )

    # 应用权重边界约束（CCD 解本身是正的 simplex，仅在用户指定更紧的 lo/hi 时投影）
    if lo > 0.0 or hi < 1.0:
        w = _project(w, lo, hi, n)
        if not np.allclose(w, np.full(n, 1.0 / n), atol=1e-6):
            warnings.append(
                "risk_parity 权重已投影到 min/max_weight 盒约束；"
                "严格等风险贡献不再保证"
            )

    return w, converged, k + 1


# ---------------------------------------------------------------------------
# 约束投影与辅助
# ---------------------------------------------------------------------------

def _project(v: np.ndarray, lo: float, hi: float, n: int) -> np.ndarray:
    """欧氏投影到 ``{w : lo ≤ w_i ≤ hi, Σw = 1}``。

    无盒约束（``lo ≤ 0`` 且 ``hi ≥ 1``）时退化为标准概率单纯形投影，
    使用 O(n log n) 排序算法精确求解。
    否则使用二分搜索 Lagrange 乘子 θ 使 ``Σ clip(v_i − θ, lo, hi) = 1``。
    """
    if lo <= 0.0 and hi >= 1.0:
        return _project_simplex(v)
    return _project_bounded_simplex(v, lo, hi)


def _project_simplex(v: np.ndarray) -> np.ndarray:
    """欧氏投影到概率单纯形 ``{w ≥ 0, Σw = 1}`` (Wang & Carreira-Perpiñán, 2013)."""
    n = v.shape[0]
    u = np.sort(v)[::-1]
    cssv = np.cumsum(u) - 1.0
    ind = np.arange(1, n + 1, dtype=float)
    cond = u - cssv / ind > 0
    rho_idx = np.nonzero(cond)[0]
    if rho_idx.size == 0:
        return np.full(n, 1.0 / n)
    rho = rho_idx[-1]
    theta = cssv[rho] / (rho + 1)
    return np.maximum(v - theta, 0.0)


def _project_bounded_simplex(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """欧氏投影到 ``{w : lo ≤ w_i ≤ hi, Σw = 1}``（二分搜索 Lagrange 乘子）."""
    n = v.shape[0]
    # 不可行检查（应由调用方保证，但兜底）
    if lo * n > 1.0 + 1e-9 or hi * n < 1.0 - 1e-9:
        return np.full(n, 1.0 / n)

    # θ 的搜索范围：θ → −∞ 时 clip(v−θ) = lo，θ → +∞ 时 = hi
    theta_lo = float(v.min()) - hi - 1.0
    theta_hi = float(v.max()) - lo + 1.0

    for _ in range(100):
        theta = (theta_lo + theta_hi) / 2.0
        w = np.clip(v - theta, lo, hi)
        s = w.sum()
        if s > 1.0:
            theta_lo = theta
        else:
            theta_hi = theta

    theta = (theta_lo + theta_hi) / 2.0
    w = np.clip(v - theta, lo, hi)
    # 修正浮点残余
    residual = 1.0 - w.sum()
    if abs(residual) > 1e-12:
        # 将残余分配到未饱和（严格在 (lo,hi) 内）的坐标
        unsat = np.where((w > lo + 1e-15) & (w < hi - 1e-15))[0]
        if unsat.size > 0:
            w[unsat] += residual / unsat.size
            w = np.clip(w, lo, hi)
    return w


def _check_bounds_feasible(
    n: int, lo: float, hi: float
) -> tuple[bool, list[str]]:
    """检查盒约束 ``{lo ≤ w_i ≤ hi, Σw = 1}`` 是否可行."""
    warnings: list[str] = []
    if lo < 0:
        warnings.append(f"min_weight={lo} < 0；已截断为 0（long-only）")
        lo = 0.0
    if hi > 1:
        warnings.append(f"max_weight={hi} > 1；已截断为 1")
        hi = 1.0
    if lo > hi:
        warnings.append(f"min_weight={lo} > max_weight={hi}；约束不可行，使用等权")
        return False, warnings
    if lo * n > 1.0 + 1e-9:
        warnings.append(
            f"min_weight={lo} × N={n} = {lo*n:.4f} > 1；约束不可行，使用等权"
        )
        return False, warnings
    if hi * n < 1.0 - 1e-9:
        warnings.append(
            f"max_weight={hi} × N={n} = {hi*n:.4f} < 1；约束不可行，使用等权"
        )
        return False, warnings
    return True, warnings


def _snap_to_simplex(
    w: np.ndarray, lo: float, hi: float, n: int
) -> np.ndarray:
    """将浮点权重精确归一到 simplex（修正累积浮点误差）."""
    w = np.asarray(w, dtype=float)
    w = np.clip(w, lo if lo > 0 else 0.0, hi if hi < 1 else 1.0)
    s = w.sum()
    if s > 0:
        w = w / s
    else:
        w = np.full(n, 1.0 / n)
    return w


def _empty_result(method: str, warnings: list[str]) -> OptimizationResult:
    """构造非法输入的空结果."""
    return OptimizationResult(
        status="invalid_input",
        weights=np.array([], dtype=float),
        expected_return=0.0,
        volatility=0.0,
        sharpe=0.0,
        converged=False,
        iterations=0,
        warnings=warnings,
        method=method,
    )


def _derive_status(warnings: list[str]) -> str:
    """从警告列表推断最终 status：有数值降级警告则为 degraded，否则 ok."""
    degraded_keywords = ("退化", "不可行", "未收敛", "≤ 0", "非正")
    for w in warnings:
        if any(kw in w for kw in degraded_keywords):
            return "degraded"
    return "ok"


def _build_result(
    status: str,
    weights: np.ndarray,
    returns: np.ndarray,
    risk_free_rate: float,
    *,
    converged: bool,
    iterations: int,
    warnings: list[str],
    method: str,
) -> OptimizationResult:
    """计算诊断指标并组装 OptimizationResult."""
    r = np.asarray(returns, dtype=float)
    if r.ndim == 2 and r.shape[0] >= 1 and weights.size == r.shape[1]:
        mu = r.mean(axis=0)
        cov = np.atleast_2d(np.cov(r, rowvar=False)) if r.shape[0] >= 2 else np.zeros((r.shape[1], r.shape[1]))
        exp_ret = float(weights @ mu)
        var = float(weights @ cov @ weights) if cov.shape == (weights.size, weights.size) else 0.0
        vol = float(np.sqrt(max(var, 0.0)))
    else:
        exp_ret = 0.0
        vol = 0.0

    if vol > 1e-15:
        sharpe = (exp_ret - risk_free_rate) / vol
    else:
        sharpe = 0.0

    return OptimizationResult(
        status=status,
        weights=weights,
        expected_return=exp_ret,
        volatility=vol,
        sharpe=sharpe,
        converged=converged,
        iterations=iterations,
        warnings=warnings,
        method=method,
    )
