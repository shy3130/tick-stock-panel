"""策略详情 _strategy_detail 触发器合并回归测试.

回归点 (bug): entry_signals / exit_signals 原先直接返回策略源文件默认值,
没有合并 overrides, 导致用户在卡片弹窗里新选的买卖触发器 tag 保存后回显丢失.

契约:
  - 无 overrides  → 返回策略默认 entry_signals / exit_signals
  - 有 overrides  → 返回 overrides 里保存的 list (即使为空, 也代表用户主动清空)
"""
from __future__ import annotations

from app.api.strategy import _strategy_detail
from app.strategy.engine import StrategyDef


def _make_strategy(
    entry_signals: list[str],
    exit_signals: list[str],
) -> StrategyDef:
    """构造最小可用的 StrategyDef (只填必填字段)."""
    return StrategyDef(
        meta={"id": "test_strat", "name": "测试策略"},
        basic_filter={"enabled": True},
        entry_signals=list(entry_signals),
        exit_signals=list(exit_signals),
        stop_loss=-0.08,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=10,
        alerts=[],
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=60,
        source="builtin",
    )


def test_no_overrides_returns_default_signals():
    """无 overrides 时返回策略源文件默认的触发器."""
    s = _make_strategy(
        entry_signals=["signal_ma20_breakout", "signal_n_day_high"],
        exit_signals=["signal_ma20_breakdown"],
    )
    detail = _strategy_detail(s, overrides=None)
    assert detail["entry_signals"] == ["signal_ma20_breakout", "signal_n_day_high"]
    assert detail["exit_signals"] == ["signal_ma20_breakdown"]


def test_overrides_signals_are_reflected():
    """核心回归: 用户保存了更多触发器, 详情必须回显保存值而非默认值."""
    s = _make_strategy(
        entry_signals=["signal_ma20_breakout"],
        exit_signals=["signal_ma20_breakdown"],
    )
    overrides = {
        "entry_signals": ["signal_ma20_breakout", "signal_macd_golden", "signal_n_day_high"],
        "exit_signals": ["signal_ma20_breakdown", "signal_macd_dead"],
    }
    detail = _strategy_detail(s, overrides=overrides)
    assert detail["entry_signals"] == overrides["entry_signals"]
    assert detail["exit_signals"] == overrides["exit_signals"]


def test_empty_override_signals_reflected():
    """用户主动清空所有触发器 → 保存空 list, 详情应回显空 (而非回退默认)."""
    s = _make_strategy(
        entry_signals=["signal_ma20_breakout"],
        exit_signals=["signal_ma20_breakdown"],
    )
    detail = _strategy_detail(s, overrides={"entry_signals": [], "exit_signals": []})
    assert detail["entry_signals"] == []
    assert detail["exit_signals"] == []


def test_partial_override_keeps_other_default():
    """只覆盖 entry_signals, exit_signals 保持默认 (key 不在 overrides 里)."""
    s = _make_strategy(
        entry_signals=["signal_ma20_breakout"],
        exit_signals=["signal_ma20_breakdown"],
    )
    detail = _strategy_detail(s, overrides={"entry_signals": ["signal_x"]})
    assert detail["entry_signals"] == ["signal_x"]
    assert detail["exit_signals"] == ["signal_ma20_breakdown"]
