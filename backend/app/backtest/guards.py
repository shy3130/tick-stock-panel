"""优化/验证任务共享护栏: 主目标白名单与最小交易数门槛。

一处定义, 稳健优化 (robust_optimizer) 与滚动样本外检验 (walkforward) 共用;
optimizer.py 的 VALID_OBJECTIVES 已收敛到此 (P4)。设计见 docs/robust-optimizer-plan.md 第四节。
"""

from __future__ import annotations

# 可作主目标的指标: 方向语义明确、跨参数组可比、不随交易数退化。
# max_drawdown / mc_maxdd_* 为负值, 最大化带符号值 = 回撤越小越好。
PRIMARY_OBJECTIVES: frozenset[str] = frozenset({
    "sortino", "sharpe", "calmar",
    "annual_return", "total_return", "profit_factor",
    "max_drawdown", "mc_maxdd_p50", "mc_maxdd_p95",
    "avg_pnl", "median_pnl",
})

# 退化目标: 交易数少时可被运气推到极端 (3 笔全胜 = win_rate 100%), 只作展示列
# 或约束条件, 不得作主排名目标。
DEGENERATE_OBJECTIVES: frozenset[str] = frozenset({
    "win_rate", "avg_holding_days", "n_trades",
})


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
