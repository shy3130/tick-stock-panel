"""向量化因子算子库 — 纯 Polars 实现的 Alpha Zoo 表达式算子。

移植来源
--------
``../Vibe-Trading/agent/src/factors/base.py`` —— 该模块用 pandas/numpy 在
**宽表**（index=date, columns=symbol）上实现 Alpha Zoo 的算子原语。本模块将
其逐算子翻译为 **Polars 表达式**，适配 tickflow 的 **长表** panel
（列含 ``symbol`` / ``date`` + OHLCV/amount 等数值列），并保留上游的语义约定：

* **NaN 传播**：每个算子都传播空值，不做 ``fillna(0)``；常量窗口的
  ``ts_corr`` / ``ts_cov`` 返回 null，而非静默的 0。
* **min_periods = n**：所有 ``ts_*`` 滚动算子在 warmup（窗口未填满）期返回
  null；窗口内出现任何 null，该窗口结果为 null。
* **Lookahead 禁令**：``ts_delta`` / ``ts_delay`` 强制 lag ``>= 1``；
  不提供负向 shift（``Ref(df, -n)``）形式。
* **符号约定**：``ts_*`` 返回的值由最近的 bar 出发向后看（因果）。

调用约定
--------
* **时序算子**（``ts_*`` / ``decay_linear`` / ``signed_power``）返回一个
  ``pl.Expr``，**不自带分组**。调用方须按 symbol 分组，惯用 ``.pipe()``
  链式组合并收尾 ``.over("symbol")``::

      pl.col("close").pipe(ts_mean, 5).pipe(ts_rank, 3).over("symbol")

* **横截面算子**（``rank`` / ``scale``）按交易日分组，内部已应用
  ``.over(date_col)``，直接使用即可。

组合规则（重要）：不要把已带 ``.over("symbol")`` 的表达式**直接嵌进**横截面
算子（两层 ``over`` 叠加是 Polars 未定义行为，会得到全 null）。时序→横截面
的跨界组合请分两步 ``with_columns``：先把时序量物化成具体列，再对该列应用
``rank`` / ``scale``。


纯 Polars 实现策略
------------------
* ``ts_mean`` / ``ts_std`` / ``ts_sum`` / ``ts_max`` / ``ts_min`` 直接映射到
  Polars 原生 ``rolling_*``（默认 ``min_samples = window_size``，warmup 即
  null；``rolling_std`` / ``rolling_var`` 为样本方差 ddof=1，与上游一致）。
* ``ts_corr`` / ``ts_regression`` 通过滚动矩推导：样本协方差
  ``cov = (E[xy] - E[x]E[y]) * n/(n-1)``，分母用样本方差（ddof=1），除数为 0
  时返回 null。
* ``ts_rank`` / ``decay_linear`` 用 ``shift`` 累加实现（窗口内任一 null 使整
  个窗口为 null，完美对齐上游语义）。
"""

from __future__ import annotations

import polars as pl

__all__ = [
    "rank",
    "scale",
    "ts_rank",
    "ts_corr",
    "ts_std",
    "ts_mean",
    "ts_delta",
    "ts_delay",
    "ts_sum",
    "ts_max",
    "ts_min",
    "decay_linear",
    "signed_power",
    "ts_regression",
]


# --------------------------------------------------------------------------- #
# 横截面算子（按交易日分组）
# --------------------------------------------------------------------------- #
def rank(expr: pl.Expr, date_col: str = "date") -> pl.Expr:
    """横截面百分位 rank（ties=average, pct, 保留 NaN）。

    等价于 pandas ``df.rank(axis=1, method="average", pct=True,
    na_option="keep")``：NaN 输入保持 NaN，全 NaN 行返回全 NaN 行。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::rank``。
    """
    avg_rank = expr.rank(method="average").over(date_col)
    # 交易日内的有效（非空）计数，用于把 1-based 平均名次归一到 (0, 1]。
    valid_count = expr.is_not_null().sum().over(date_col)
    return avg_rank / valid_count


def scale(expr: pl.Expr, a: float = 1.0, date_col: str = "date") -> pl.Expr:
    """横截面 L1 归一化：使每个交易日内绝对值之和等于 ``a``。

    绝对值之和为 0（全 0 或全 NaN）的行返回 NaN —— 绝不静默归零。
    等价于 pandas ``df.mul(a).div(abs_sum, axis=0)``。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::scale``。
    """
    abs_sum = expr.abs().sum().over(date_col)
    return (expr * a) / abs_sum


# --------------------------------------------------------------------------- #
# 时序算子（返回裸 Expr，调用方收尾 .over("symbol")）
# --------------------------------------------------------------------------- #
def ts_mean(expr: pl.Expr, n: int) -> pl.Expr:
    """滚动均值，warmup（前 n-1 行）返回 null。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::ts_mean``。
    """
    if n < 1:
        raise ValueError(f"ts_mean window must be >= 1, got {n}")
    return expr.rolling_mean(window_size=n)


def ts_std(expr: pl.Expr, n: int) -> pl.Expr:
    """滚动样本标准差（ddof=1），warmup 返回 null。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::ts_std``。
    """
    if n < 2:
        raise ValueError(f"ts_std window must be >= 2, got {n}")
    return expr.rolling_std(window_size=n)


def ts_sum(expr: pl.Expr, n: int) -> pl.Expr:
    """滚动求和，warmup 返回 null。

    对应 Alpha101 中的 ``Sum(x, n)``；上游未单列此算子，按同族 ts_* 语义补齐。
    """
    if n < 1:
        raise ValueError(f"ts_sum window must be >= 1, got {n}")
    return expr.rolling_sum(window_size=n)


def ts_max(expr: pl.Expr, n: int) -> pl.Expr:
    """滚动最大值，warmup 返回 null。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::ts_max``。
    """
    if n < 1:
        raise ValueError(f"ts_max window must be >= 1, got {n}")
    return expr.rolling_max(window_size=n)


def ts_min(expr: pl.Expr, n: int) -> pl.Expr:
    """滚动最小值，warmup 返回 null。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::ts_min``。
    """
    if n < 1:
        raise ValueError(f"ts_min window must be >= 1, got {n}")
    return expr.rolling_min(window_size=n)


def ts_delta(expr: pl.Expr, d: int) -> pl.Expr:
    """一阶差分 ``expr - expr.shift(d)``（lag d 的差）。

    Lookahead 禁令：强制 ``d >= 1``。
    移植自 ``Vibe-Trading/agent/src/factors/base.py::delta``。
    """
    if d < 1:
        raise ValueError(f"ts_delta lag must be >= 1 (lookahead ban), got {d}")
    return expr - expr.shift(d)


def ts_delay(expr: pl.Expr, d: int) -> pl.Expr:
    """向后位移 ``expr.shift(d)``（取 d 期前的值）。

    对应 Alpha101 中的 ``Ts_Delay / Ref``；Lookahead 禁令：强制 ``d >= 1``。
    上游未单列此算子，按同族语义补齐。
    """
    if d < 1:
        raise ValueError(f"ts_delay lag must be >= 1 (lookahead ban), got {d}")
    return expr.shift(d)


def signed_power(expr: pl.Expr, p: float) -> pl.Expr:
    """``sign(expr) * |expr| ** p`` —— 保留符号，永不产生复数。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::signed_power``。
    """
    return expr.sign() * expr.abs().pow(p)


def decay_linear(expr: pl.Expr, n: int) -> pl.Expr:
    """线性衰减加权移动平均，权重 ``n, n-1, ..., 1``（归一化）。

    最近一期权重最大（= n / sum）；warmup（前 n-1 行）返回 null；窗口内任一
    null 使该窗口结果为 null。因果对齐：output[i] 只依赖 input[i-n+1 : i+1]。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::decay_linear``（上游用
    numpy sliding_window_view + einsum；这里改用 ``shift`` 加权累加，全程留在
    Polars 表达式空间内）。

    权重对齐说明：上游实现把 ``np.arange(n,0,-1)`` 直接乘到
    ``sliding_window_view`` 的窗口上（窗口 index 0 = 最旧），实际给最旧一期最
    大权重 —— 与标准 Alpha101 ``decay_linear``（最近一期权重最大）及算子命名
    相悖。本实现按标准 Alpha101 对齐：``shift(0)``（最近一期）取权重 ``n``。
    """
    if n < 1:
        raise ValueError(f"decay_linear window must be >= 1, got {n}")
    weights = list(range(n, 0, -1))  # n, n-1, ..., 1
    total = sum(weights)
    acc: pl.Expr | None = None
    for k, w in enumerate(weights):  # k=0 为最近一期，权重 n
        term = expr.shift(k) * (w / total)
        acc = term if acc is None else acc + term
    # acc 不会为 None：n>=1 保证至少一次循环。
    return acc  # type: ignore[return-value]


def ts_rank(expr: pl.Expr, n: int) -> pl.Expr:
    """滚动时序 rank（窗口内**最新值**的百分位名次）。

    结果为 (0, 1] 的百分位，与横截面 ``rank`` 可组合。ties 用 average 名次；
    warmup（前 n-1 行）返回 null；窗口内任一 null、或最新值本身为 null 时，
    该窗口返回 null（min_periods = n）。

    算法：把窗口内每一期与最新值比较，累加 ``less``（严格小于最新值）与
    ``eq``（等于最新值）计数，名次 = ``less + 0.5*(eq+1)``，百分位 = 名次 / n。
    全程在 Polars 表达式空间内完成（n 个 ``shift`` 累加），无需 ``map_elements``。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::ts_rank``。
    """
    if n < 1:
        raise ValueError(f"ts_rank window must be >= 1, got {n}")

    less: pl.Expr | None = None
    eq: pl.Expr | None = None
    valid: pl.Expr | None = None
    for k in range(n):
        shifted = expr.shift(k)
        less_term = (shifted < expr).cast(pl.Int64).fill_null(0)
        eq_term = (shifted == expr).cast(pl.Int64).fill_null(0)
        nn_term = shifted.is_not_null().cast(pl.Int64)
        less = less_term if less is None else less + less_term
        eq = eq_term if eq is None else eq + eq_term
        valid = nn_term if valid is None else valid + nn_term
    rank_avg = less + 0.5 * (eq + 1)  # type: ignore[operator]
    # 仅当窗口填满（valid == n）且最新值非空时输出百分位，否则 null。
    return (
        pl.when((valid == n) & expr.is_not_null())  # type: ignore[operator]
        .then(rank_avg / n)
        .otherwise(None)
    )


def ts_corr(x: pl.Expr, y: pl.Expr, n: int) -> pl.Expr:
    """滚动样本 Pearson 相关系数，min_periods = n。

    常量序列窗口（分母为 0）返回 null —— 不静默归零。与
    ``pandas.rolling(n).corr`` 一致。

    实现用滚动矩推导：样本协方差
    ``cov = (E[xy] - E[x]E[y]) * n/(n-1)``，分母用样本方差
    ``rolling_var``（ddof=1），保证分子分母除数一致。Polars 未提供
    ``Expr.rolling_corr``，故以此方式补齐。

    移植自 ``Vibe-Trading/agent/src/factors/base.py::ts_corr``。
    """
    if n < 2:
        raise ValueError(f"ts_corr window must be >= 2, got {n}")
    mxy = (x * y).rolling_mean(window_size=n)
    mx = x.rolling_mean(window_size=n)
    my = y.rolling_mean(window_size=n)
    cov = (mxy - mx * my) * (n / (n - 1))
    vx = x.rolling_var(window_size=n)
    vy = y.rolling_var(window_size=n)
    denom = (vx * vy).sqrt()
    return pl.when(denom > 0).then(cov / denom).otherwise(None)


def ts_regression(y: pl.Expr, x: pl.Expr, n: int) -> pl.Expr:
    """滚动 OLS 一元回归残差 ``y - (a + b*x)``。

    在每个长度为 n 的窗口内，用最小二乘拟合 ``y = a + b*x``，返回**残差**
    （实际值 - 拟合值），对应 Alpha101 #106 ``Ts_Residual`` 类因子。warmup
    返回 null；自变量方差为 0（x 恒定）的窗口返回 null。

    斜率 ``b = cov_sample(x,y) / var_sample(x)``，截距 ``a = mean_y - b*mean_x``。
    全程滚动矩推导，留在 Polars 表达式空间内。上游未提供此算子，按 Alpha101
    标准定义补齐。
    """
    if n < 2:
        raise ValueError(f"ts_regression window must be >= 2, got {n}")
    mxy = (x * y).rolling_mean(window_size=n)
    mx = x.rolling_mean(window_size=n)
    my = y.rolling_mean(window_size=n)
    cov_sample = (mxy - mx * my) * (n / (n - 1))
    vx = x.rolling_var(window_size=n)
    slope = pl.when(vx > 0).then(cov_sample / vx).otherwise(None)
    intercept = my - slope * mx
    fitted = intercept + slope * x
    return y - fitted
