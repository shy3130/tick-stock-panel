"""参数网格搜索优化器。

给定策略 + 参数网格, 遍历所有参数组合各跑一次回测, 按目标指标排序, 返回最优参数。

- 参数网格校验对齐 StrategyDef.meta["params"] (类型/范围/选项)。
- 多线程并行执行, 复用 PanelCache: 同一 symbols/日期的面板只加载一次, 其余组合命中缓存。
- 支持进度回调 (第 i/N 组完成) 与取消。
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

# 组合数硬上限 — 防止参数网格爆炸 (每组一次回测, 过大直接拒绝)。
GRID_MAX_COMBINATIONS = 2000

# 需最小化的目标 (值越小越好); 其余默认最大化。
# 注意: max_drawdown / mc_maxdd_* 为负值, 最大化其带符号值 = 回撤越小越好, 故仍归为 max。
_MINIMIZE_OBJECTIVES = {"avg_holding_days"}

# 可选优化目标 (须为 stats 中存在且数值可比的字段)。
VALID_OBJECTIVES = {
    "total_return", "annual_return", "sharpe", "sortino", "calmar",
    "win_rate", "profit_factor", "max_drawdown", "mc_maxdd_p50", "mc_maxdd_p95",
    "avg_pnl", "median_pnl", "n_trades", "avg_holding_days",
}


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
            raise ValueError(f"参数 '{param_id}' 为 {p_type} 型, 不支持 min/max/step 展开, 请给候选值列表")
        step = spec.get("step") or pmeta.get("step")
        if step is None or float(step) <= 0:
            raise ValueError(f"参数 '{param_id}' 的 step 必须为正数")
        lo = float(spec.get("min", pmeta.get("min", 0)))
        hi = float(spec.get("max", pmeta.get("max", 0)))
        if hi < lo:
            raise ValueError(f"参数 '{param_id}' 的 max < min")
        step = float(step)
        # 整数计数生成候选, 避免浮点累加误差丢端点 (如 0.1/0.1 步长)。
        n_steps = round((hi - lo) / step)
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
                raise ValueError(f"参数 '{param_id}' 的候选值 {val} 超出范围 (< min {pmeta['min']})")
            if pmeta.get("max") is not None and num > float(pmeta["max"]) + 1e-9:
                raise ValueError(f"参数 '{param_id}' 的候选值 {val} 超出范围 (> max {pmeta['max']})")
            out.append(round(num) if p_type == "int" else num)
        elif p_type == "bool":
            out.append(bool(val))
        elif p_type == "select":
            if val not in pmeta.get("options", []):
                raise ValueError(f"参数 '{param_id}' 的候选值 {val!r} 不在 options {pmeta.get('options')} 中")
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
        raise ValueError(f"参数组合数 {total} 超过上限 {GRID_MAX_COMBINATIONS}, 请增大 step 或缩小范围")

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


# optimize 显式传入的 StrategyBacktestConfig 参数, backtest_kwargs 不得重复覆盖。
_RESERVED_BT_KEYS = {"strategy_id", "symbols", "start", "end", "params", "overrides"}


def _validate_backtest_kwargs(backtest_kwargs: dict) -> None:
    """校验 backtest_kwargs 的 key 合法且不与显式参数冲突, 否则会在 worker 线程抛 TypeError。"""
    from dataclasses import fields

    from app.backtest.strategy import StrategyBacktestConfig

    valid = {f.name for f in fields(StrategyBacktestConfig)} - _RESERVED_BT_KEYS
    for k in backtest_kwargs:
        if k in _RESERVED_BT_KEYS:
            raise ValueError(f"backtest_kwargs 不能包含 '{k}' (由优化器显式管理)")
        if k not in valid:
            raise ValueError(f"backtest_kwargs 含非法字段 '{k}', 合法: {sorted(valid)}")


@dataclass
class OptimizeConfig:
    strategy_id: str
    symbols: list[str] | None
    start: date
    end: date
    param_grid: dict
    objective: str = "sortino"
    direction: str | None = None  # None -> 由 objective 推断
    max_workers: int = 4
    base_params: dict = field(default_factory=dict)   # 不扫的固定策略参数
    overrides: dict | None = None
    backtest_kwargs: dict = field(default_factory=dict)  # matching/fees/mode/initial_capital 等


class StrategyOptimizer:
    """遍历参数组合并行回测, 按目标排序。"""

    def __init__(self, service, strategy_engine) -> None:
        self.service = service
        self.strategy_engine = strategy_engine

    def optimize(
        self,
        cfg: OptimizeConfig,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        from app.backtest.strategy import StrategyBacktestConfig

        t0 = time.perf_counter()
        if cfg.objective not in VALID_OBJECTIVES:
            raise ValueError(f"不支持的优化目标 '{cfg.objective}', 可选: {sorted(VALID_OBJECTIVES)}")
        direction = cfg.direction or default_direction(cfg.objective)
        _validate_backtest_kwargs(cfg.backtest_kwargs)

        s = self.strategy_engine.get(cfg.strategy_id)  # 可能抛 ValueError
        params_meta = s.meta.get("params", [])
        combos = expand_param_grid(params_meta, cfg.param_grid)
        n_total = len(combos)

        results: list[dict] = []
        done = 0
        lock = threading.Lock()

        def _run_one(idx: int, combo: dict) -> dict | None:
            if cancel_event is not None and cancel_event.is_set():
                return None
            # 单组异常必须隔离: 加了并行后, 一组抛异常若冒泡会拖垮整批 (丢弃全部已完成结果)。
            try:
                merged = {**cfg.base_params, **combo}
                bt_cfg = StrategyBacktestConfig(
                    strategy_id=cfg.strategy_id,
                    symbols=cfg.symbols,
                    start=cfg.start,
                    end=cfg.end,
                    params=merged,
                    overrides=cfg.overrides,
                    **cfg.backtest_kwargs,
                )
                res = self.service.run(bt_cfg, cancel_event=cancel_event)
            except Exception as e:  # 隔离单组失败, 记录后继续, 不拖垮整批
                logger.warning("参数组 %s 回测异常: %r", combo, e)
                return {"params": combo, "error": repr(e), "objective_raw": None, "_sort": float("-inf")}
            if res.error:
                return {"params": combo, "error": res.error, "objective_raw": None, "_sort": float("-inf")}
            # _sort: 内部排序键 (统一"越大越好"); objective_raw: 原始展示值 (不受方向取负污染)。
            return {
                "params": combo,
                "objective_raw": res.stats.get(cfg.objective),
                "_sort": objective_value(res.stats, cfg.objective, direction),
                "stats": res.stats,
            }

        def _best_raw() -> float | None:
            if not results:
                return None
            top = max(results, key=lambda x: x["_sort"])
            return None if top["_sort"] == float("-inf") else top.get("objective_raw")

        max_workers = max(1, min(int(cfg.max_workers), n_total))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_one, i, c): i for i, c in enumerate(combos)}
            for fut in as_completed(futures):
                r = fut.result()  # _run_one 内部已兜底, 不会 re-raise 业务异常
                with lock:
                    done += 1
                    if r is not None:
                        results.append(r)
                    if progress_cb is not None:
                        br = _best_raw()
                        progress_cb({
                            "type": "optimizer_progress",
                            "done": done,
                            "total": n_total,
                            "best_score": round(br, 4) if br is not None else None,
                        })

        # 排序: 内部 _sort 降序 (越大越好); -inf (失败/无效) 沉底。展示层用 objective_raw。
        ranked = sorted(results, key=lambda x: x["_sort"], reverse=True)
        for i, r in enumerate(ranked):
            r["rank"] = i + 1
            r.pop("_sort", None)  # 不外露内部排序键, 避免展示层误用取负值

        best = ranked[0] if ranked and ranked[0].get("objective_raw") is not None else None
        best_raw = best["objective_raw"] if best else None
        return {
            "objective": cfg.objective,
            "direction": direction,
            "n_combinations": n_total,
            "n_completed": len(results),
            "best_params": best["params"] if best else None,
            "best_score": round(best_raw, 4) if best_raw is not None else None,
            "results": ranked,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
