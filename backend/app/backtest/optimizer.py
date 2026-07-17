"""参数网格搜索优化器。

给定策略 + 参数网格, 遍历所有参数组合各跑一次回测, 按目标指标排序, 返回最优参数。

- 参数网格校验对齐 StrategyDef.meta["params"] (类型/范围/选项)。
- 单个优化任务在一个 worker 内串行执行; matrix_native 策略共享一份 MarketDataMatrix。
- 支持进度回调 (第 i/N 组完成) 与取消。
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from typing import Protocol

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
    matrix_cache_max_mb: int = 512


class PhaseRssSampler(Protocol):
    """Resource probe supplied by the outer worker process."""

    def reset_phase(self) -> None: ...

    def phase_peak_rss_bytes(self) -> int: ...


class StrategyOptimizer:
    """在单 worker 内遍历参数组合, 并按目标排序。"""

    def __init__(self, service, strategy_engine) -> None:
        self.service = service
        self.strategy_engine = strategy_engine

    def optimize(
        self,
        cfg: OptimizeConfig,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
        *,
        rss_sampler: PhaseRssSampler | None = None,
        prepared_market_data=None,
    ) -> dict:
        from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

        t0 = time.perf_counter()
        if cfg.objective not in VALID_OBJECTIVES:
            raise ValueError(f"不支持的优化目标 '{cfg.objective}', 可选: {sorted(VALID_OBJECTIVES)}")
        direction = cfg.direction or default_direction(cfg.objective)
        _validate_backtest_kwargs(cfg.backtest_kwargs)
        if int(cfg.matrix_cache_max_mb) <= 0:
            raise ValueError("matrix_cache_max_mb 必须为正整数")

        s = self.strategy_engine.get(cfg.strategy_id)  # 可能抛 ValueError
        params_meta = s.meta.get("params", [])
        combos = expand_param_grid(params_meta, cfg.param_grid)
        n_total = len(combos)

        results: list[dict] = []
        backtest_configs = [
            StrategyBacktestConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=cfg.start,
                end=cfg.end,
                params={**cfg.base_params, **combo},
                overrides=cfg.overrides,
                **cfg.backtest_kwargs,
            )
            for combo in combos
        ]

        prepared = None
        prepare_ms = 0.0
        trials_ms = 0.0
        final_backtest_ms = 0.0
        trial_peak_rss_bytes = None
        final_backtest_peak_rss_bytes = None
        cache_summary = None
        output: dict = {}
        trial_policy = BacktestResultPolicy.optimizer_trial(cfg.objective)

        def _run_one(combo: dict, bt_cfg: StrategyBacktestConfig) -> dict | None:
            if cancel_event is not None and cancel_event.is_set():
                return None
            # 单组异常必须隔离: 一组失败不能丢弃全部已完成结果。
            try:
                if prepared is None:
                    res = self.service.run(
                        bt_cfg,
                        cancel_event=cancel_event,
                        result_policy=trial_policy,
                    )
                else:
                    res = self.service.run(
                        bt_cfg,
                        cancel_event=cancel_event,
                        prepared=prepared,
                        result_policy=trial_policy,
                    )
            except Exception as e:  # 隔离单组失败, 记录后继续, 不拖垮整批
                logger.warning("参数组 %s 回测异常: %r", combo, e)
                return {"params": combo, "error": repr(e), "objective_raw": None, "_sort": float("-inf")}
            if res.error:
                return {"params": combo, "error": res.error, "objective_raw": None, "_sort": float("-inf")}
            if cfg.objective not in res.stats:
                return {
                    "params": combo,
                    "error": f"回测结果缺少优化目标字段 '{cfg.objective}'",
                    "objective_raw": None,
                    "_sort": float("-inf"),
                }
            # _sort: 内部排序键 (统一"越大越好"); objective_raw: 原始展示值 (不受方向取负污染)。
            objective_started = time.perf_counter()
            sort_value = objective_value(res.stats, cfg.objective, direction)
            objective_ms = round((time.perf_counter() - objective_started) * 1000, 3)
            return {
                "params": combo,
                "objective_raw": res.stats.get(cfg.objective),
                "_sort": sort_value,
                "stats": res.stats,
                "objective_evaluation_ms": objective_ms,
            }

        def _best_raw() -> float | None:
            if not results:
                return None
            top = max(results, key=lambda x: x["_sort"])
            return None if top["_sort"] == float("-inf") else top.get("objective_raw")

        try:
            cancelled_before_prepare = cancel_event is not None and cancel_event.is_set()
            if (
                getattr(s, "execution_backend", "polars_expr") == "matrix_native"
                and not cancelled_before_prepare
            ):
                prepare_started = time.perf_counter()
                prepare_kwargs = {
                    "matrix_cache_max_bytes": int(cfg.matrix_cache_max_mb) * 1024 * 1024,
                }
                if prepared_market_data is not None:
                    prepare_kwargs["market_data_override"] = prepared_market_data
                prepared = self.service.prepare_matrix_optimization(
                    backtest_configs,
                    **prepare_kwargs,
                )
                prepare_ms = round((time.perf_counter() - prepare_started) * 1000, 1)
                if progress_cb is not None:
                    progress_cb({
                        "type": "optimizer_prepare",
                        "done": 0,
                        "total": n_total,
                        "best_score": None,
                        "shared_matrix_bytes": prepared.market_data.nbytes,
                        "elapsed_ms": prepare_ms,
                    })

            trials_started = time.perf_counter()
            if rss_sampler is not None:
                rss_sampler.reset_phase()
            for done, (combo, bt_cfg) in enumerate(
                zip(combos, backtest_configs, strict=True),
                start=1,
            ):
                r = _run_one(combo, bt_cfg)
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
                if cancel_event is not None and cancel_event.is_set():
                    break
            trials_ms = round((time.perf_counter() - trials_started) * 1000, 1)
            if rss_sampler is not None:
                trial_peak_rss_bytes = rss_sampler.phase_peak_rss_bytes()

            ranked = sorted(results, key=lambda x: x["_sort"], reverse=True)
            for i, result_row in enumerate(ranked):
                result_row["rank"] = i + 1
                result_row.pop("_sort", None)

            best = ranked[0] if ranked and ranked[0].get("objective_raw") is not None else None
            best_raw = best["objective_raw"] if best else None
            best_backtest = None
            if best is not None and not (cancel_event is not None and cancel_event.is_set()):
                if progress_cb is not None:
                    progress_cb({
                        "type": "optimizer_finalize",
                        "done": len(results),
                        "total": n_total,
                        "best_score": round(best_raw, 4) if best_raw is not None else None,
                    })
                best_config = StrategyBacktestConfig(
                    strategy_id=cfg.strategy_id,
                    symbols=cfg.symbols,
                    start=cfg.start,
                    end=cfg.end,
                    params={**cfg.base_params, **best["params"]},
                    overrides=cfg.overrides,
                    **cfg.backtest_kwargs,
                )
                final_started = time.perf_counter()
                if rss_sampler is not None:
                    rss_sampler.reset_phase()
                final_result = self.service.run(
                    best_config,
                    cancel_event=cancel_event,
                    prepared=prepared,
                )
                final_backtest_ms = round((time.perf_counter() - final_started) * 1000, 1)
                if rss_sampler is not None:
                    final_backtest_peak_rss_bytes = rss_sampler.phase_peak_rss_bytes()
                best_backtest = asdict(final_result) if is_dataclass(final_result) else dict(final_result)

            trials_per_second = (
                round(len(results) / (trials_ms / 1000.0), 4)
                if trials_ms > 0
                else 0.0
            )
            output = {
                "objective": cfg.objective,
                "direction": direction,
                "n_combinations": n_total,
                "n_completed": len(results),
                "best_params": best["params"] if best else None,
                "best_score": round(best_raw, 4) if best_raw is not None else None,
                "best_backtest": best_backtest,
                "results": ranked,
                "requested_max_workers": int(cfg.max_workers),
                "effective_workers": 1,
                "shared_market_data": prepared is not None,
                "shared_market_data_bytes": prepared.market_data.nbytes if prepared is not None else 0,
                "prepare_ms": prepare_ms,
                "timing_ms": {
                    "prepare": prepare_ms,
                    "trials": trials_ms,
                    "best_backtest": final_backtest_ms,
                },
                "performance": {
                    "mode": "serial",
                    "trials_per_second": trials_per_second,
                    "trial_peak_rss_bytes": trial_peak_rss_bytes,
                    "best_backtest_peak_rss_bytes": final_backtest_peak_rss_bytes,
                    "parallel_evaluated": False,
                },
            }
        finally:
            if prepared is not None:
                try:
                    cache_summary = prepared.compute_cache.snapshot()
                finally:
                    prepared.compute_cache.close()

        if cache_summary is not None:
            cache_summary["released"] = True
            cache_summary["current_bytes_after_close"] = 0
            output["matrix_compute_cache"] = cache_summary
        output["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        output["timing_ms"]["total"] = output["elapsed_ms"]
        return output
