"""叠加策略合并器 — 选股与回测共用的纯函数。

为什么单独成模块: 选股(StrategyEngine._run_composite_strategy)和回测
(StrategyBacktestService)都要合并子策略结果, 必须共享同一套口径, 否则会出现
"选股与回测使用不同逻辑"的金融错误(CONTRIBUTING §5.1)。

两种合并入口:
- merge_results: 选股合并。输入各子 StrategyResult, 输出合并后的 StrategyResult。
- merge_signal_matrices: 回测合并。输入各子 SignalMatrix, 输出合并后的 SignalMatrix。

合并语义(首版):
- entry: union=OR(entries); intersect=Σ(entries) >= min_confirm
- score: 各子内部按 score 降序排名归一到 [0,1], 命中子策略间按权重加权。
         排名是相对位置, 跨子策略天然可比, 不依赖各子 per-strategy 的 min-max 量纲。
- exit(回测): 来源投影。每个子策略 i 的 exit 仅在它自己 entry 后的持仓窗口内生效,
              避免"B 的退出信号平掉 A 的仓位"。窗口由全局 max_hold 封顶。

退出投影的金融正确性: 现有撮合引擎(engine.py:993)读全局 matrix.exit, 不区分来源。
若直接 OR(exit) 会产生"幽灵平仓"(B 把 A 选的仓位卖掉)。来源投影在合并器(撮合前)
解决, 撮合层零改动。
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import numpy as np

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
    results: list[StrategyResult],
    children_weights: list[float],
    merge_mode: str,
    min_confirm: int,
    *,
    as_of: date,
    strategy_id: str,
    elapsed_ms: float = 0.0,
) -> StrategyResult:
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


def _hold_masks_from_entries(entries: list[np.ndarray], max_hold: int) -> list[np.ndarray]:
    """计算每个子策略的持仓窗口掩码。

    hold_mask_i[t, a] = True 当且仅当存在 t' <= t 使 entry_i[t', a] 触发,
    且 t - t' < max_hold(即仍在最长持仓期内, 未被 max_hold 强制平仓)。
    实现用前向填充: 从每个 entry 起向前扩展 max_hold-1 个 bar 为 True。
    """
    if max_hold <= 0:
        max_hold = 1
    masks: list[np.ndarray] = []
    for entry in entries:
        raw = entry.astype(bool, copy=False)
        # 对每个 asset 列, 把 True 向前传播 max_hold 个 bar。
        # 用按行位移 OR 实现: mask[t] |= raw[t-k] for k in [0, max_hold-1]
        mask = np.zeros_like(raw)
        window = raw.copy()
        mask |= window
        for _k in range(1, max_hold):
            window = np.roll(window, 1, axis=0)
            window[0, :] = False  # roll 会在顶部环绕, 置零防未来泄漏
            mask |= window
        masks.append(mask)
    return masks


def merge_signal_matrices(
    shape: tuple[int, int],
    sigs: list,
    children: list[tuple[str, float]],
    merge_mode: str,
    min_confirm: int,
    max_hold: int,
):
    """回测合并: 产出合并 entry/exit/score/entry_signal_code 矩阵。

    Args:
        shape: (n_times, n_assets)
        sigs: 各子策略 SignalMatrix(顺序与 children 对齐)
        children: [(strategy_id, weight), ...]
        merge_mode / min_confirm: 同 merge_results
        max_hold: 全局最长持仓天数, 用于退出投影窗口封顶

    返回合并后的 SignalMatrix。
    """
    from app.backtest.matrix import make_signal_matrix

    n_times, n_assets = shape
    n_children = len(sigs)
    if n_children == 0:
        return make_signal_matrix(shape)

    # ── entry ──
    entries = [np.asarray(s.entry, dtype=bool) for s in sigs]
    entry_stack = np.stack(entries, axis=0)  # (n_children, n_times, n_assets)
    if merge_mode == "intersect":
        effective_min = max(min_confirm, 1) if min_confirm and min_confirm > 0 else n_children
        confirm_count = entry_stack.sum(axis=0)  # (n_times, n_assets)
        merged_entry = confirm_count >= effective_min
    else:  # union
        merged_entry = entry_stack.any(axis=0)

    # ── exit 来源投影 ──
    # 每个子策略的 exit 仅在自己持仓窗口内生效, 不串平其他子的仓位。
    hold_masks = _hold_masks_from_entries(entries, max_hold)
    merged_exit = np.zeros(shape, dtype=bool)
    for i, s in enumerate(sigs):
        child_exit = np.asarray(s.exit, dtype=bool)
        merged_exit |= child_exit & hold_masks[i]

    # ── score 标准化排名加权 ──
    # 对每个 (time, asset), 在命中的子策略间按权重加权各自的内部排名归一值。
    weights = np.array([w for _, w in children], dtype=np.float64)
    # 预计算每个子策略、每个 time 上 asset 的排名归一。
    norm_scores = np.zeros((n_children, n_times, n_assets), dtype=np.float64)
    for i, s in enumerate(sigs):
        raw = np.asarray(s.score, dtype=np.float64)
        for t in range(n_times):
            row = raw[t]
            hit = entries[i][t]
            if not hit.any():
                continue
            hit_idx = np.flatnonzero(hit)
            hit_vals = row[hit_idx]
            finite = np.isfinite(hit_vals)
            if not finite.any():
                norm_scores[i, t, hit_idx[finite]] = _NEUTRAL_NORM
                continue
            valid_idx = hit_idx[finite]
            valid_vals = hit_vals[finite]
            n = len(valid_idx)
            if n <= 1:
                norm_scores[i, t, valid_idx] = _NEUTRAL_NORM
                continue
            # 降序排名: 最优=1, 最差=0。
            order = np.argsort(-valid_vals, kind="stable")
            ranks = np.empty(n, dtype=np.float64)
            ranks[order] = np.arange(1, n + 1, dtype=np.float64)
            normalized = 1 - (ranks - 1) / (n - 1)
            norm_scores[i, t, valid_idx] = normalized

    hit_mask = entry_stack  # (n_children, n_times, n_assets)
    hit_weights = np.where(hit_mask, weights.reshape(-1, 1, 1), 0.0)
    weight_sum = hit_weights.sum(axis=0)  # (n_times, n_assets)
    blended = (norm_scores * hit_weights).sum(axis=0)
    safe_sum = np.where(weight_sum > 0, weight_sum, 1.0)
    merged_score = np.where(merged_entry, blended / safe_sum, 0.0) * 100
    merged_score = np.nan_to_num(merged_score, nan=0.0, posinf=0.0, neginf=0.0)

    # ── entry_signal_code: 来源标记 ──
    # code = 命中的第一个子策略索引; -1 表示无命中。归因用。
    entry_codes = np.full(shape, -1, dtype=np.int16)
    for i in range(n_children):
        # 只给尚未标记的命中点打 code(第一个命中优先), 避免覆盖。
        untagged = (entry_codes == -1) & entries[i]
        entry_codes[untagged] = i

    # exit_signal_code 沿用 -1(无独立信号来源); 回测按 "signal" reason 归因即可。
    exit_codes = np.full(shape, -1, dtype=np.int16)

    # entry_signal_ids 映射表: code i → "composite:child_id"
    entry_signal_ids = tuple(f"composite:{cid}" for cid, _ in children)

    return make_signal_matrix(
        shape,
        entry=merged_entry.astype(np.uint8),
        exit=merged_exit.astype(np.uint8),
        score=merged_score.astype(np.float32),
        entry_signal_code=entry_codes,
        exit_signal_code=exit_codes,
        entry_signal_ids=entry_signal_ids,
    )
