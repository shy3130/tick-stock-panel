"""回测 TradeRecord.cause_tag 归因标签测试 (最小侵入: 默认值 + 映射函数)。"""
from __future__ import annotations

from datetime import date

from app.backtest.engine import TradeRecord, cause_tag_for

# 现有全部退出原因 (见 engine.TradeRecord.exit_reason 注释 + 撮合路径)
KNOWN_REASONS = (
    "signal",
    "stop_loss",
    "take_profit",
    "trailing_stop",
    "trailing_take_profit",
    "max_hold",
    "end",
)


def _record(**kw) -> TradeRecord:
    base = dict(
        symbol="A",
        entry_date=date(2024, 1, 1),
        exit_date=date(2024, 1, 3),
        entry_price=10.0,
        exit_price=11.0,
        pnl_pct=0.1,
        duration=2,
        exit_reason="signal",
    )
    base.update(kw)
    return TradeRecord(**base)


# ── cause_tag_for 映射 ──────────────────────────────────
def test_known_reasons_map_to_strategy_outcome():
    for r in KNOWN_REASONS:
        assert cause_tag_for(r) == "strategy_outcome", r


def test_unknown_reason_maps_to_driver_quality():
    assert cause_tag_for("data_gap") == "driver_quality"
    assert cause_tag_for("") == "driver_quality"
    assert cause_tag_for("halt_unmapped") == "driver_quality"


# ── TradeRecord 默认值 ──────────────────────────────────
def test_default_cause_tag_is_strategy_outcome():
    assert _record().cause_tag == "strategy_outcome"


def test_cause_tag_assignable():
    t = _record(cause_tag="driver_quality")
    assert t.cause_tag == "driver_quality"


def test_cause_tag_independent_of_exit_reason_default():
    # 即便 exit_reason 未知, 未显式传 cause_tag 时仍取默认 strategy_outcome
    t = _record(exit_reason="halt_unmapped")
    assert t.cause_tag == "strategy_outcome"
