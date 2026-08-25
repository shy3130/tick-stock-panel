"""叠加策略合并器 — 选股合并的纯函数。

为什么单独成模块: 选股(StrategyEngine._run_composite_strategy)和回测
(StrategyBacktestService)都要合并子策略结果, 必须共享同一套口径, 否则会出现
"选股与回测使用不同逻辑"的金融错误。

本模块只负责选股合并(merge_results): 输入各子 StrategyResult, 输出合并后的
StrategyResult。回测合并在 app.backtest.strategy 内以 polars 掩码原生实现
(当前回测是掩码驱动, 非矩阵驱动), 复用本模块的排名融合语义。

合并语义(首版):
- entry: union=OR(entries); intersect=Σ(entries) >= min_confirm
- score: 各子内部按 score 降序排名归一到 [0,1], 命中子策略间按权重加权。
         排名是相对位置, 跨子策略天然可比, 不依赖各子 per-strategy 的 min-max 量纲。
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.strategy.engine import StrategyResult

# 中性分: 子策略无 score 或单候选(无法排名)时的占位, 不污染融合结果。
_NEUTRAL_NORM = 0.5


def _effective_weights(
    children_weights: list[float],
    hits_mask: list[bool],
) -> tuple[list[float], float]:
    """从权重列表中筛出命中子策略的权重, 返回(命中权重列表, 命中权重总和)。"""
    effective = [w for w, hit in zip(children_weights, hits_mask, strict=True) if hit]
    total = sum(effective)
    return effective, total


def merge_results(
    results: "list[StrategyResult]",
    children_weights: list[float],
    merge_mode: str,
    min_confirm: int,
    *,
    as_of: date,
    strategy_id: str,
    elapsed_ms: float = 0.0,
) -> "StrategyResult":
    """选股合并: 按 symbol 聚合各子结果, 标准化排名加权融合 score。

    Args:
        results: 各子策略的 StrategyResult(顺序与 children_weights 对齐)
        children_weights: 各子权重(顺序对齐)
        merge_mode: "union"(任一命中即入选) | "intersect"(至少 min_confirm 个命中)
        min_confirm: intersect 模式下命中的最少子策略数; <=0 视为全部子策略
        as_of / strategy_id: 合并结果归属(composite 自身)
    """
    from app.strategy.engine import StrategyResult

    n_children = len(results)
    if n_children == 0:
        return StrategyResult(as_of=as_of, strategy_id=strategy_id, elapsed_ms=elapsed_ms)

    # 各子的 symbol → 排名归一 score。排名基于子策略内部的原始 score 降序。
    # norm∈[0,1], 最优标的=1。单候选或无 score 时用中性分。
    per_child_norm: list[dict[str, float]] = []
    per_child_symbols: list[set[str]] = []
    for res in results:
        symbols = set(res.scores.keys())
        per_child_symbols.append(symbols)
        norm: dict[str, float] = {}
        if symbols:
            ordered = sorted(symbols, key=lambda s: res.scores[s], reverse=True)
            count = len(ordered)
            for rank, sym in enumerate(ordered, start=1):
                norm[sym] = 1 - (rank - 1) / max(count - 1, 1)
        else:
            # 子策略未产出 score: 命中即中性分, 不奖励也不惩罚。
            for row in res.rows:
                sym = str(row.get("symbol"))
                if sym and sym not in norm:
                    norm[sym] = _NEUTRAL_NORM
        per_child_norm.append(norm)

    # 确定入围标的集合 + 各标的的命中子策略索引。
    universe: set[str] = set()
    for syms in per_child_symbols:
        universe.update(syms)
    # 也纳入 rows 里有但 scores 里没有的标的(子策略无 score 但产出行)。
    for res in results:
        for row in res.rows:
            sym = str(row.get("symbol"))
            if sym:
                universe.add(sym)

    effective_min = max(min_confirm, 1) if min_confirm and min_confirm > 0 else n_children
    scores: dict[str, float] = {}
    for sym in universe:
        hits = [i for i in range(n_children) if sym in per_child_norm[i]]
        if not hits:
            continue
        if merge_mode == "intersect" and len(hits) < effective_min:
            continue
        weights, total_w = _effective_weights(
            children_weights, [i in hits for i in range(n_children)]
        )
        if total_w <= 0:
            # 全部权重为 0: 退化为均等。
            total_w = float(len(hits))
            weights = [1.0] * len(hits)
        blended = sum(w * per_child_norm[i][sym] for i, w in zip(hits, weights, strict=True))
        scores[sym] = round(blended / total_w * 100, 4)

    total = len(scores)
    return StrategyResult(
        as_of=as_of,
        strategy_id=strategy_id,
        rows=[],
        total=total,
        elapsed_ms=elapsed_ms,
        scores=scores,
    )
