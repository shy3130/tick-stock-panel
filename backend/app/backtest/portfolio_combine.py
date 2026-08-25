"""组合级回测 — 多个已固化 Run 日频净值的事后加权合成 (F15)。

口径声明（显式，勿改）：
    本模块把若干**独立回测**的日频净值曲线在共同交易日历上按权重**事后
    加权合成**，输出组合级净值与指标。它**不是共享资金池撮合**——不模拟
    策略之间的资金竞争、同时满仓冲突、保证金占用或任何资金约束；再平衡
    假设无摩擦（无手续费/滑点/冲击成本）。成分 Run 各自的成交可行性已在
    其独立回测中裁决，本模块不再复核。

对齐规则：
    - 仅接受 position 模式的策略 Run（kind=strategy/composite 且有日频
      equity_curve）；候选执行模式（stats.full_kind=candidate_execution）
      与因子 Run 不具备日频净值语义，直接拒绝并指出是哪个 run。
    - 各 Run 净值按日期取交集；共同交易日 < 20 视为无法对齐，拒绝。
    - 成分收益在**相邻共同交易日之间**计算（跨缺口日的收益复合计入下一
      个共同交易日），因此各成分在共同窗口上的复合收益严格等于其首末
      净值之比。
    - 权重归一化为和 1；原始和不为 1 时以 warning 注明原值，不静默。

再平衡语义（账户追踪法，无除零路径）：
    每个成分一个虚拟账户，初始资金 = 归一化权重；每日账户按该成分收益
    增值；再平衡日（daily=每日 / monthly=每月首个共同交易日开盘）把各
    账户重置为目标权重份额。组合净值 = 各账户之和，恒为正。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Literal, Sequence

import numpy as np

from app.backtest.metrics import (
    MetricContext,
    annualized_sharpe,
    performance_metrics,
)
from app.backtest.run_store import BacktestRun

__all__ = [
    "MAX_COMBINE_ITEMS",
    "MIN_COMBINE_ITEMS",
    "MIN_OVERLAP_DAYS",
    "PortfolioCombineError",
    "combine_run_equities",
]

MAX_COMBINE_ITEMS = 8
MIN_COMBINE_ITEMS = 2
MIN_OVERLAP_DAYS = 20

RebalanceMode = Literal["daily", "monthly", "none"]
REBALANCE_MODES: tuple[str, ...] = ("daily", "monthly", "none")

# 口径提示：合成结果仅供组合结构研究，不得当作可执行账户回测。
METHODOLOGY_WARNING = (
    "口径: 组合净值由独立回测的日频净值事后加权合成，非共享资金池撮合——"
    "不模拟策略间资金竞争或同时满仓冲突；再平衡假设无摩擦（无手续费/滑点）。"
)
_RISK_FREE_WARNING = (
    "合成指标按日频 MetricContext (risk_free=0) 计算，不继承各成分 run 的无风险利率设置"
)


class PortfolioCombineError(ValueError):
    """组合合成不可进行 (API 层映射 422) — str(e) 为中文原因。"""


def _run_series(run: BacktestRun) -> dict[str, float]:
    """Run 日频净值 → {iso日期: 正有限值}；旧引擎 equity 键与新 value 键均兼容。"""
    out: dict[str, float] = {}
    for row in run.equity_curve or []:
        if not isinstance(row, dict):
            continue
        d = str(row.get("date") or "")[:10]
        if len(d) != 10:
            continue
        raw = row.get("value")
        if raw is None:
            raw = row.get("equity")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0.0:
            out[d] = value
    return out


def _reject_unusable(run: BacktestRun, series: dict[str, float]) -> None:
    """仅 position 模式策略 Run 可参与：候选执行 / 因子 / 无曲线一律拒绝。"""
    rid = run.run_id
    if run.kind == "factor":
        raise PortfolioCombineError(
            f"run {rid} 为因子回测，无日频净值曲线，无法参与组合合成"
        )
    if run.kind not in ("strategy", "composite"):
        raise PortfolioCombineError(
            f"run {rid} 的 kind={run.kind!r} 不属于策略回测，无法参与组合合成"
        )
    if (run.stats or {}).get("full_kind") == "candidate_execution":
        raise PortfolioCombineError(
            f"run {rid} 为候选执行模式（mode=full），净值按退出事件日采样，"
            "不具备日频语义，无法参与组合合成；请使用仓位模拟的 run"
        )
    if len(series) < 2:
        raise PortfolioCombineError(
            f"run {rid} 无日频净值曲线（有效净值点少于 2 个），无法参与组合合成"
        )


def _month_changed(prev_iso: str, curr_iso: str) -> bool:
    return prev_iso[:7] != curr_iso[:7]


def _correlation(a: np.ndarray, b: np.ndarray) -> float | None:
    """两列日收益的 Pearson 相关；零方差（收益恒定）返回 None，不伪造数值。"""
    sa = float(np.std(a, ddof=1))
    sb = float(np.std(b, ddof=1))
    if sa <= 0.0 or sb <= 0.0:
        return None
    ca = a - float(np.mean(a))
    cb = b - float(np.mean(b))
    denom = float(np.sqrt(np.sum(ca * ca) * np.sum(cb * cb)))
    if denom <= 0.0:
        return None
    value = float(np.sum(ca * cb)) / denom
    return value if math.isfinite(value) else None


def combine_run_equities(
    items: Sequence[tuple[BacktestRun, float]],
    rebalance: RebalanceMode = "daily",
) -> dict:
    """把多个已固化策略 Run 的日频净值合成为组合级净值与指标。

    输入 ``items`` 为 (Run, 原始权重) 序列（2~8 个）；输出契约见模块
    docstring 的口径声明。所有不可对齐/不可合成情形抛
    ``PortfolioCombineError``（中文原因，API 层映射 422），fail-closed
    不伪造数值。纯 numpy 后处理，不触发行情与网络。
    """
    if rebalance not in REBALANCE_MODES:
        raise PortfolioCombineError(f"不支持的再平衡模式: {rebalance!r}（须为 daily/monthly/none）")
    if not (MIN_COMBINE_ITEMS <= len(items) <= MAX_COMBINE_ITEMS):
        raise PortfolioCombineError(
            f"组合成分数量须在 {MIN_COMBINE_ITEMS}~{MAX_COMBINE_ITEMS} 之间，当前 {len(items)} 个"
        )

    seen: set[str] = set()
    for run, _ in items:
        if run.run_id in seen:
            raise PortfolioCombineError(f"run_id 重复: {run.run_id}")
        seen.add(run.run_id)

    # ── 各 Run 净值序列与逐项拒绝 ──────────────────────────
    series_list: list[dict[str, float]] = []
    for run, _ in items:
        series = _run_series(run)
        _reject_unusable(run, series)
        series_list.append(series)

    common = sorted(set.intersection(*(set(s) for s in series_list)))
    if len(common) < MIN_OVERLAP_DAYS:
        raise PortfolioCombineError(
            f"{len(items)} 个 Run 日频净值共同交易日仅 {len(common)} 天 (< {MIN_OVERLAP_DAYS})，"
            "无法对齐计算组合净值"
        )
    n_days = len(common)

    # ── 权重校验与归一化 ────────────────────────────────────
    warnings: list[str] = [METHODOLOGY_WARNING, _RISK_FREE_WARNING]
    raw_weights: list[float] = []
    for run, weight in items:
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) \
                or not math.isfinite(float(weight)) or float(weight) < 0.0:
            raise PortfolioCombineError(f"run {run.run_id} 权重非法（须为非负有限数）")
        raw_weights.append(float(weight))
    total_weight = math.fsum(raw_weights)
    if total_weight <= 0.0:
        raise PortfolioCombineError("权重总和必须大于 0")
    if abs(total_weight - 1.0) > 1e-9:
        warnings.append(f"权重原始和为 {total_weight:.6g}，已归一化为 1 继续计算")
    weights = np.asarray([w / total_weight for w in raw_weights], dtype=float)

    # ── 共同日历上的成分日收益 ──────────────────────────────
    rets = np.empty((len(items), n_days - 1), dtype=float)
    for i, series in enumerate(series_list):
        for k in range(1, n_days):
            rets[i, k - 1] = series[common[k]] / series[common[k - 1]] - 1.0

    # ── 账户追踪合成组合净值 ────────────────────────────────
    accts = weights.copy()
    values: list[float] = [float(accts.sum())]
    for k in range(1, n_days):
        if rebalance == "daily" or (
            rebalance == "monthly" and _month_changed(common[k - 1], common[k])
        ):
            accts = weights * float(accts.sum())
        accts = accts * (1.0 + rets[:, k - 1])
        values.append(float(accts.sum()))
    equity_curve = [
        {"date": d, "value": round(v, 6)} for d, v in zip(common, values)
    ]

    # ── 组合级指标 (MetricContext 全套) ─────────────────────
    portfolio_returns = np.asarray(values[1:], dtype=float) / np.asarray(
        values[:-1], dtype=float
    ) - 1.0
    stats = performance_metrics(
        returns=portfolio_returns, context=MetricContext("daily")
    )
    stats["total_return"] = values[-1] / values[0] - 1.0

    # ── 成分明细: 各自收益/夏普 + 对组合增量的贡献 ──────────
    total_gain = values[-1] - values[0]
    out_items: list[dict] = []
    for i, (run, _) in enumerate(items):
        item_returns = rets[i]
        item_total = series_list[i][common[-1]] / series_list[i][common[0]] - 1.0
        contribution = (
            float((accts[i] - weights[i]) / total_gain)
            if total_gain != 0.0
            else None
        )
        label = run.label or run.subject.name or run.run_id
        out_items.append(
            {
                "run_id": run.run_id,
                "label": label,
                "weight": round(float(weights[i]), 6),
                "weight_raw": raw_weights[i],
                "total_return": item_total,
                "sharpe": annualized_sharpe(item_returns, MetricContext("daily")),
                "contribution": contribution,
            }
        )

    # ── 成分相关性矩阵 (对称, 对角 1; 零方差对 → None) ───────
    n_items = len(items)
    matrix: list[list[float | None]] = [
        [1.0 if i == j else _correlation(rets[i], rets[j]) for j in range(n_items)]
        for i in range(n_items)
    ]

    # ── 对齐损耗显式计数 (不静默丢弃) ───────────────────────
    for i, (run, _) in enumerate(items):
        dropped = len(series_list[i]) - n_days
        if dropped > 0:
            warnings.append(
                f"run {run.run_id} 有 {dropped} 个交易日不在共同交易日历内，已按共同日历对齐"
            )

    return {
        "equity_curve": equity_curve,
        "stats": stats,
        "items": out_items,
        "correlation_matrix": {
            "run_ids": [run.run_id for run, _ in items],
            "values": matrix,
        },
        "overlap_days": n_days,
        "rebalance": rebalance,
        "warnings": list(dict.fromkeys(warnings)),
        "date_range": {"start": common[0], "end": common[-1]},
        "synthesized_at": date.today().isoformat(),
    }
