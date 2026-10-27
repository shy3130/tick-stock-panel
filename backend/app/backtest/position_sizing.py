"""仓位策略契约 (PositionSizingPolicy) — 用户自定义仓位规则的纯计算契约。

用户在 设置 → 定义 → 仓位策略 写一个 ``def compute(ctx)`` 函数, 回测的
组合模式在每个买入日调用一次, 用当日及以前的信息决定:

  - ``weights``: 当日候选之间的相对资金分配 (引擎会归一化, 只看比例);
  - ``budget_scale``: 当日总买入预算的整体缩放 (0~1, 只缩不放 —
    单仓上限/现金/敞口钳制仍在引擎侧, 写错也不会爆仓)。

口径红线 (金融契约):
  - ctx 只含当日及以前的信息 (候选评分 / 最近收益 / 已平仓交易快照 /
    组合状态), 接口层面不存在未来数据入口;
  - 输出经 normalize_result 强校验: 长度=候选数、全有限值、和>0、
    budget_scale∈(0,1] — 非法输出直接拒绝 (fail-closed), 不静默给错仓位;
  - 引擎运行期 compute 抛错时当日回退等权并计数 (兜底方向), 见引擎接线。

本模块零引擎依赖: dataclass 契约 + 校验 + 合成冒烟, 独立单测; 模拟盘
将来复用同一契约 (api_version 供破坏性演化, 见 secondary-development §8)。
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

API_VERSION = 1

__all__ = [
    "API_VERSION",
    "Candidate",
    "ComputeFn",
    "PortfolioState",
    "PositionSizingContext",
    "PositionSizingResult",
    "TradeSnapshot",
    "normalize_result",
    "smoke_compute",
    "synthetic_context",
]


@dataclass(frozen=True)
class Candidate:
    """当日买入候选 (按信号强度降序)。"""

    symbol: str
    score: float


@dataclass(frozen=True)
class TradeSnapshot:
    """已平仓交易快照 — 只读历史, 供胜率/盈亏比类规则使用。"""

    symbol: str
    pnl_pct: float
    duration: int
    exit_reason: str


@dataclass(frozen=True)
class PortfolioState:
    """组合当日状态 (买入评估时点)。"""

    cash: float
    equity: float
    drawdown: float  # ≤0, (equity - peak)/peak 口径, 与引擎一致
    open_positions: int


@dataclass(frozen=True)
class PositionSizingContext:
    """compute 的唯一输入 — 全部为当日及以前的信息。"""

    selected: tuple[Candidate, ...]
    recent_returns: Mapping[str, Sequence[float]]  # 仅含当日选中标的
    closed_trades: tuple[TradeSnapshot, ...]
    portfolio: PortfolioState
    params: Mapping[str, Any]
    api_version: int = API_VERSION


@dataclass(frozen=True)
class PositionSizingResult:
    """compute 的输出 — 引擎归一化 weights 并按 budget_scale 缩放总预算。"""

    weights: Sequence[float]
    budget_scale: float = 1.0


ComputeFn = Callable[[PositionSizingContext], PositionSizingResult]


def normalize_result(
    result: PositionSizingResult, n_selected: int
) -> tuple[list[float], float]:
    """校验并归一化 compute 输出; 非法即抛 ValueError (中文, 面向用户)。

    weights 只取相对比例 (归一化到和=1), 用户不必关心量纲;
    budget_scale 钳制到 (0, 1] — 只能缩预算, 不能放大。
    """
    if not isinstance(result, PositionSizingResult):
        raise ValueError(
            f"compute 必须返回 PositionSizingResult, 得到 {type(result).__name__}"
        )
    raw = result.weights
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError("weights 必须是数值序列 (list/tuple)")
    if len(raw) != n_selected:
        raise ValueError(f"weights 长度必须等于候选数 {n_selected}, 得到 {len(raw)}")
    values: list[float] = []
    for i, w in enumerate(raw):
        try:
            v = float(w)
        except (TypeError, ValueError) as e:
            raise ValueError(f"weights[{i}] 不是数值: {w!r}") from e
        if not math.isfinite(v):
            raise ValueError(f"weights[{i}] 不是有限值: {w!r}")
        values.append(v)
    total = sum(values)
    if not (total > 0):
        raise ValueError(f"weights 的和必须大于 0, 当前合计 {total}")
    if any(v < 0 for v in values):
        # 允许小负值出现于某些优化型输出, 归零后再归一, 不让负权重进资金分配
        values = [max(v, 0.0) for v in values]
        total = sum(values)
        if total <= 0:
            raise ValueError("weights 去除负值后合计为 0")
    scale = result.budget_scale
    try:
        scale = float(scale)
    except (TypeError, ValueError) as e:
        raise ValueError(f"budget_scale 不是数值: {scale!r}") from e
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError(f"budget_scale 必须在 (0, 1], 得到 {scale}")
    scale = min(scale, 1.0)
    return [v / total for v in values], scale


def synthetic_context(n_candidates: int = 3) -> PositionSizingContext:
    """合成冒烟 context — 固定种子可复现, 不依赖任何真实数据。"""
    if n_candidates < 1:
        raise ValueError("n_candidates 必须 ≥ 1")
    scores = [0.9 - 0.1 * i for i in range(n_candidates)]
    selected = tuple(
        Candidate(symbol=f"SMOKE{i:02d}", score=s) for i, s in enumerate(scores)
    )
    step = 0.011
    returns: dict[str, list[float]] = {}
    for i in range(n_candidates):
        # 每个标的 60 天收益, 波幅随序号增大 — 覆盖"波动率目标"类规则
        vals: list[float] = []
        for j in range(60):
            phase = (i * 7 + j * 3) % 11
            vals.append(round((phase - 5) * step * (1 + 0.2 * i), 6))
        returns[f"SMOKE{i:02d}"] = vals
    trades = tuple(
        TradeSnapshot(symbol=f"T{i:02d}", pnl_pct=p, duration=5 + i, exit_reason=r)
        for i, (p, r) in enumerate(
            [(0.05, "signal"), (-0.02, "stop_loss"), (0.08, "signal"),
             (-0.01, "max_hold"), (0.03, "signal"), (0.06, "trailing_stop")]
        )
    )
    return PositionSizingContext(
        selected=selected,
        recent_returns=returns,
        closed_trades=trades,
        portfolio=PortfolioState(cash=60_000.0, equity=100_000.0, drawdown=-0.05, open_positions=4),
        params={},
    )


def smoke_compute(compute: ComputeFn) -> dict:
    """保存前的冒烟: 合成 context 实跑一次 + 输出校验。

    返回 {ok: bool, issues: [str], weights: [...], budget_scale: float};
    issues 为可读中文, 供界面直接展示。
    """
    issues: list[str] = []
    ctx = synthetic_context()
    try:
        result = compute(ctx)
    except Exception as e:  # noqa: BLE001 — 用户代码, 任何异常都要转成可读问题
        return {"ok": False, "issues": [f"compute 执行失败: {type(e).__name__}: {e}"], "weights": [], "budget_scale": 0.0}
    try:
        weights, scale = normalize_result(result, len(ctx.selected))
    except ValueError as e:
        issues.append(str(e))
        return {"ok": False, "issues": issues, "weights": [], "budget_scale": 0.0}
    return {
        "ok": True,
        "issues": [],
        "weights": [round(w, 6) for w in weights],
        "budget_scale": round(scale, 6),
    }
