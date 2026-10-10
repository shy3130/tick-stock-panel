"""回归: 移动止盈「回撤 > 激活涨幅」时显式报错, 不再静默钳制。

用户 bug 报告②: 旧实现用 min(drawdown, activate) 把用户填的回撤值静默改写为
激活值, 配置"不生效"且无任何提示。修复后该组合直接报错, 用户需调整参数。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.backtest.engine import BacktestEngine
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyEngine

REPO_ROOT = Path(__file__).resolve().parents[3]


def _service() -> StrategyBacktestService:
    return StrategyBacktestService(
        BacktestEngine(repo=None),
        StrategyEngine(strategy_dirs=[REPO_ROOT / "backend" / "tests" / "fixtures" / "strategies"]),
    )


def _config(activate, drawdown) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id="macd_golden",
        symbols=None,
        start=date(2024, 1, 1),
        end=date(2024, 3, 1),
        overrides={
            "trailing_take_profit_activate": activate,
            "trailing_take_profit_drawdown": drawdown,
        },
    )


def test_drawdown_greater_than_activate_errors():
    """回撤(10%) > 激活(5%) → 报错并点名两参数, 而非静默按 5% 跑。"""
    result = _service().run(_config(0.05, 0.10))
    assert result.error
    assert "移动止盈回撤" in result.error
    assert "激活涨幅" in result.error


def test_drawdown_not_greater_than_activate_passes_validation():
    """回撤 ≤ 激活 → 校验放行(后续数据阶段是否失败与本回归点无关)。"""
    outcome = ""
    try:
        outcome = _service().run(_config(0.10, 0.05)).error or ""
    except Exception as exc:  # noqa: BLE001 — repo=None, 数据阶段异常在所难免
        outcome = str(exc)
    assert "移动止盈回撤" not in outcome
