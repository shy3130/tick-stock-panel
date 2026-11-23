"""Shared parameter grid and scoring rules for TSP compatibility optimizers."""

from __future__ import annotations

import itertools

# 组合数硬上限 — 防止参数网格爆炸 (每组一次回测, 过大直接拒绝)。
GRID_MAX_COMBINATIONS = 2000

# 需最小化的目标 (值越小越好); 其余默认最大化。
# 注意: max_drawdown / mc_maxdd_* 为负值, 最大化其带符号值 = 回撤越小越好, 故仍归为 max。
_MINIMIZE_OBJECTIVES = {"avg_holding_days"}

# 可作主目标的指标: 方向语义明确、跨参数组可比、不随交易数退化。
# max_drawdown / mc_maxdd_* 为负值, 最大化带符号值 = 回撤越小越好。
PRIMARY_OBJECTIVES: frozenset[str] = frozenset(
    {
        "sortino",
        "sharpe",
        "calmar",
        "annual_return",
        "total_return",
        "profit_factor",
        "max_drawdown",
        "mc_maxdd_p50",
        "mc_maxdd_p95",
        "avg_pnl",
        "median_pnl",
    }
)

# 退化目标: 交易数少时可被运气推到极端 (3 笔全胜 = win_rate 100%), 只作展示列
# 或约束条件, 不得作主排名目标。
DEGENERATE_OBJECTIVES: frozenset[str] = frozenset(
    {
        "win_rate",
        "avg_holding_days",
        "n_trades",
    }
)


def validate_primary_objective(objective: str) -> None:
    """主目标必须在白名单内, 否则抛 ValueError (端点转 400)。"""
    if objective not in PRIMARY_OBJECTIVES:
        raise ValueError(
            f"'{objective}' 不适合作稳健优化的主目标 (交易数少时会退化); "
            f"可选: {sorted(PRIMARY_OBJECTIVES)}"
        )


def trades_ok(stats: dict, min_trades: int) -> bool:
    """最小交易数门槛: stats 的 n_trades 达标才有效; min_trades<=0 表示关闭。

    缺失 n_trades 按不达标处理 (fail-closed: 宁可误杀也不让无交易组合登顶)。
    """
    if min_trades is None or min_trades <= 0:
        return True
    n = stats.get("n_trades")
    return isinstance(n, (int, float)) and n >= min_trades


def _candidates_for(param_id: str, spec, pmeta: dict) -> list:
    """从 grid spec 解析某参数的候选值列表并逐个校验。

    spec 支持三种写法:
      - list: 显式候选值 [v1, v2, ...]
      - {"values": [...]}: 显式候选值
      - {"min", "max", "step"}: 数值型按步长展开 (含端点)
    """
    p_type = pmeta["type"]

    # 解析原始候选值
    if isinstance(spec, list):
        raw = spec
    elif isinstance(spec, dict) and "values" in spec:
        raw = spec["values"]
    elif isinstance(spec, dict):
        if p_type not in ("float", "int"):
            raise ValueError(
                f"参数 '{param_id}' 为 {p_type} 型, 不支持 min/max/step 展开, 请给候选值列表"
            )
        step = spec.get("step") or pmeta.get("step")
        if step is None or float(step) <= 0:
            raise ValueError(f"参数 '{param_id}' 的 step 必须为正数")
        lo = float(spec.get("min", pmeta.get("min", 0)))
        hi = float(spec.get("max", pmeta.get("max", 0)))
        if hi < lo:
            raise ValueError(f"参数 '{param_id}' 的 max < min")
        step = float(step)
        # 整数计数生成候选, 避免浮点累加误差丢端点 (如 0.1/0.1 步长)。
        # 步数向下取整: (hi-lo) 不是 step 整数倍时, 四舍五入会多造一个越过 hi 的候选
        # (1~20 步长 7 → 22), 用户填的上限反而被越界校验拒绝。1e-9 容差保住整除端点。
        n_steps = int((hi - lo) / step + 1e-9)
        raw = [round(lo + i * step, 10) for i in range(n_steps + 1)]
    else:
        raise ValueError(f"参数 '{param_id}' 的网格 spec 必须是列表或 {{min,max,step}} 字典")

    if not raw:
        raise ValueError(f"参数 '{param_id}' 的候选值为空")

    # 逐值校验 + 归一化类型
    out = []
    for val in raw:
        if p_type in ("float", "int"):
            try:
                num = float(val)
            except (TypeError, ValueError):
                raise ValueError(f"参数 '{param_id}' 的候选值 {val!r} 不是数字") from None
            if pmeta.get("min") is not None and num < float(pmeta["min"]) - 1e-9:
                raise ValueError(
                    f"参数 '{param_id}' 的候选值 {val} 超出范围 (< min {pmeta['min']})"
                )
            if pmeta.get("max") is not None and num > float(pmeta["max"]) + 1e-9:
                raise ValueError(
                    f"参数 '{param_id}' 的候选值 {val} 超出范围 (> max {pmeta['max']})"
                )
            out.append(round(num) if p_type == "int" else num)
        elif p_type == "bool":
            out.append(bool(val))
        elif p_type == "select":
            if val not in pmeta.get("options", []):
                raise ValueError(
                    f"参数 '{param_id}' 的候选值 {val!r} 不在 options {pmeta.get('options')} 中"
                )
            out.append(val)
        else:
            out.append(val)
    # 去重保序
    seen = set()
    uniq = []
    for v in out:
        k = (type(v).__name__, v)
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return uniq


def _grid_candidates(params_meta: list[dict], param_grid: dict) -> dict[str, list]:
    """校验整个 param_grid, 返回 {param_id: [候选值...]}。"""
    if not param_grid:
        raise ValueError("参数网格为空, 至少需要一个可扫参数")
    by_id = {p["id"]: p for p in params_meta}
    result: dict[str, list] = {}
    for pid, spec in param_grid.items():
        if pid not in by_id:
            raise ValueError(f"参数 '{pid}' 在该策略中不存在")
        result[pid] = _candidates_for(pid, spec, by_id[pid])
    return result


def count_combinations(params_meta: list[dict], param_grid: dict) -> int:
    """组合总数 (笛卡尔积), 用于爆炸预判。"""
    cands = _grid_candidates(params_meta, param_grid)
    total = 1
    for vals in cands.values():
        total *= len(vals)
    return total


def expand_param_grid(params_meta: list[dict], param_grid: dict) -> list[dict]:
    """校验并展开为参数组合列表, 每个组合是 {param_id: value} (仅含被扫参数)。

    超过 GRID_MAX_COMBINATIONS 直接拒绝。
    """
    cands = _grid_candidates(params_meta, param_grid)
    total = 1
    for vals in cands.values():
        total *= len(vals)
    if total > GRID_MAX_COMBINATIONS:
        raise ValueError(
            f"参数组合数 {total} 超过上限 {GRID_MAX_COMBINATIONS}, 请增大 step 或缩小范围"
        )

    keys = list(cands.keys())
    combos = []
    for values in itertools.product(*(cands[k] for k in keys)):
        combos.append(dict(zip(keys, values, strict=True)))
    return combos


def objective_value(stats: dict, objective: str, direction: str) -> float:
    """从 stats 提取目标值并转为"越大越好"的可比分数 (None/缺失 -> 最差)。"""
    raw = stats.get(objective)
    if raw is None:
        return float("-inf")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float("-inf")
    if v != v or v in (float("inf"), float("-inf")):  # nan/inf
        return float("-inf")
    return -v if direction == "min" else v


def default_direction(objective: str) -> str:
    return "min" if objective in _MINIMIZE_OBJECTIVES else "max"
