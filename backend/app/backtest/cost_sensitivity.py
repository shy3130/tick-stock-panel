"""成本敏感性分析 — 同一策略在不同交易成本倍数下的表现对比。

服务层模块: 回测执行通过 run_fn 注入 (API 层接线), 本模块只负责
配置复制、倍数作用与结果抽取, 不触碰引擎内部。fail-closed:
run_fn 抛异常整体透传; 指标缺失/非有限一律输出 None, 不伪造 0。
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Callable, Iterable

from app.backtest.strategy import StrategyBacktestConfig

# 默认成本倍数: 0.0=零成本上限, 1.0=基线, >1 为压力档。
DEFAULT_COST_MULTIPLIERS: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 5.0)

BASELINE_MULTIPLIER = 1.0

COST_SENSITIVITY_NOTE = (
    "成本倍数作用于双边费用与滑点；信号与选股不受成本影响，仓位模拟下资金约束可能传导"
)


def _extract_stats(result: Any) -> dict:
    """从 run_fn 结果取 stats dict — dataclass (属性 .stats) 或 dict (键 stats) 均适配。

    缺失或非 dict → 空 dict: 字段抽取端逐个 fail-closed 为 None, 不在这里伪造。
    """
    stats = getattr(result, "stats", None)
    if stats is None and isinstance(result, dict):
        stats = result.get("stats")
    return stats if isinstance(stats, dict) else {}


def _finite(value: Any) -> float | None:
    """有限数值 → float; None/bool/非数值/非有限 (NaN, ±inf) → None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _finite_int(value: Any) -> int | None:
    """有限数值 → int (计数类字段); 否则 None。"""
    v = _finite(value)
    return None if v is None else int(v)


def _normalize_multipliers(multipliers: Iterable[float]) -> list[float]:
    """倍数校验与规整: 非负有限、去重、保证含 1.0 基线、升序。

    负数/NaN/±inf → ValueError (fail-closed, 不静默丢弃)。
    """
    normalized: list[float] = []
    seen: set[float] = set()
    for raw in multipliers:
        try:
            m = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"成本倍数必须是数值: {raw!r}") from exc
        if not math.isfinite(m) or m < 0.0:
            raise ValueError(f"成本倍数必须为非负有限数: {raw!r}")
        if m not in seen:
            seen.add(m)
            normalized.append(m)
    if BASELINE_MULTIPLIER not in seen:
        normalized.append(BASELINE_MULTIPLIER)
    return sorted(normalized)


def _total_cost(stats: dict) -> float | None:
    """stats.cost_breakdown.total → 有限数值; 无 cost_breakdown / 非有限 → None。"""
    breakdown = stats.get("cost_breakdown")
    if not isinstance(breakdown, dict):
        return None
    return _finite(breakdown.get("total"))


def _sensitivity_row(
    multiplier: float,
    fees_pct: float,
    slippage_bps: float,
    stats: dict,
) -> dict:
    """单个倍数档的结果行。所有指标经有限性过滤, 非有限值 → None (JSON-safe)。"""
    # 引擎 stats 用 annual_return; 兼容 annualized_return 命名。
    annual = stats.get("annual_return")
    if annual is None:
        annual = stats.get("annualized_return")
    return {
        "multiplier": multiplier,
        "fees_pct": round(fees_pct, 12),
        "slippage_bps": round(slippage_bps, 12),
        "is_baseline": multiplier == BASELINE_MULTIPLIER,
        "total_return": _finite(stats.get("total_return")),
        "annualized_return": _finite(annual),
        "sharpe": _finite(stats.get("sharpe")),
        "max_drawdown": _finite(stats.get("max_drawdown")),
        "final_equity": _finite(stats.get("final_equity")),
        "total_cost": _total_cost(stats),
        "n_trades": _finite_int(stats.get("n_trades")),
    }


def run_cost_sensitivity(
    run_fn: Callable[[StrategyBacktestConfig], Any],
    cfg: StrategyBacktestConfig,
    multipliers: Iterable[float] = DEFAULT_COST_MULTIPLIERS,
) -> dict:
    """成本敏感性分析: 对 fees_pct/slippage_bps 同乘倍数后逐档回测。

    - cfg 必须是带 fees_pct/slippage_bps 的回测配置 (StrategyBacktestConfig);
      每档用 dataclasses.replace 复制后修改, 原 cfg 不被改动。
    - multiplier=0.0 表示零成本 (费用与滑点均归零)。
    - run_fn(modified_cfg) 返回带 stats 的结果 (dataclass 或 dict 均可);
      任一档抛异常则整体透传, 不静默丢行。
    """
    if not (hasattr(cfg, "fees_pct") and hasattr(cfg, "slippage_bps")):
        raise TypeError(
            "cfg 需为带 fees_pct/slippage_bps 的回测配置 (StrategyBacktestConfig), "
            f"实际为 {type(cfg).__name__}"
        )
    muls = _normalize_multipliers(multipliers)
    base_fees = float(cfg.fees_pct)
    base_slippage = float(cfg.slippage_bps)

    rows: list[dict] = []
    for m in muls:
        scenario = dataclasses.replace(cfg, fees_pct=base_fees * m, slippage_bps=base_slippage * m)
        result = run_fn(scenario)  # 异常整体透传 (fail-closed)
        rows.append(
            _sensitivity_row(
                multiplier=m,
                fees_pct=scenario.fees_pct,
                slippage_bps=scenario.slippage_bps,
                stats=_extract_stats(result),
            )
        )
    return {
        "multipliers": muls,
        "rows": rows,
        "note": COST_SENSITIVITY_NOTE,
    }
