"""Legacy daily composite declarations and signal merging."""

from dataclasses import dataclass
from typing import Any

import numpy as np

from .matrix import SignalMatrix, make_signal_matrix

MAX_COMPOSITE_CHILDREN = 8
_NEUTRAL_NORM = 0.5


@dataclass(frozen=True)
class CompositeChild:
    """叠加策略的一个子策略引用。"""

    strategy_id: str
    weight: float


@dataclass(frozen=True)
class CompositeSpec:
    """叠加策略的子策略声明（无业务代码，仅引用与权重）。

    引用合法性与一致性由 _load_all 两阶段校验保证：子策略必须存在、
    非嵌套、asset_types 一致、数量 ≤ MAX_COMPOSITE_CHILDREN。
    """

    children: tuple[CompositeChild, ...]


def parse_composite_children(raw: Any) -> CompositeSpec:
    """解析 META["children"] 为 CompositeSpec。

    每项形如 {"strategy_id": "xxx", "weight": 0.4}。仅做结构和权重校验:
    - 非空 list, 每项含合法 strategy_id 与非负 weight
    - 数量 <= MAX_COMPOSITE_CHILDREN (超出在加载期拒绝, 避免信号计算成本爆炸)
    子策略的存在性/非嵌套/asset_types 一致性由 _load_all 两阶段校验保证。
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("composite strategy META['children'] must be a non-empty list")
    if len(raw) > MAX_COMPOSITE_CHILDREN:
        raise ValueError(
            f"composite strategy children count {len(raw)} exceeds limit {MAX_COMPOSITE_CHILDREN}"
        )
    children: list[CompositeChild] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"composite children[{i}] must be a dict")
        cid = item.get("strategy_id")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"composite children[{i}] missing non-empty 'strategy_id'")
        if cid in seen:
            raise ValueError(f"composite children[{i}] duplicate strategy_id {cid!r}")
        seen.add(cid)
        weight = item.get("weight", 1.0)
        try:
            weight = float(weight)
        except (TypeError, ValueError) as e:
            raise ValueError(f"composite children[{i}] weight must be a number") from e
        if weight < 0:
            raise ValueError(f"composite children[{i}] weight must be >= 0")
        children.append(CompositeChild(strategy_id=cid, weight=weight))
    return CompositeSpec(children=tuple(children))


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
    sigs: list[SignalMatrix],
    children: list[tuple[str, float]],
    merge_mode: str,
    min_confirm: int,
    max_hold: int,
) -> SignalMatrix:
    """回测合并: 产出合并 entry/exit/score/entry_signal_code 矩阵。

    Args:
        shape: (n_times, n_assets)
        sigs: 各子策略 SignalMatrix(顺序与 children 对齐)
        children: [(strategy_id, weight), ...]
        merge_mode / min_confirm: union 或 intersect，非正交集阈值表示全部
        max_hold: 全局最长持仓天数, 用于退出投影窗口封顶

    返回合并后的 SignalMatrix。
    """
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
