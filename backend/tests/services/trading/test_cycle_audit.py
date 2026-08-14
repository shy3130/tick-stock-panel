"""周期审计跨笔聚合测试。"""
from __future__ import annotations

import json
from pathlib import Path

from app.services.trading import store
from app.services.trading.cycle_audit import run_cycle_audit, _aggregate_red_flags, _aggregate_autopsies


def _make_trade(data_dir: Path, trade_id: str, *, status="已平仓", strategy="boll_breakout", pnl=0.0):
    """创建一笔最小可审计的 trade + 事件流。"""
    trade = {
        "tradeId": trade_id,
        "symbol": "000001",
        "name": "Test",
        "status": status,
        "strategy": strategy,
        "position": {"qty": 100, "costPrice": 10.0, "invested": 1000.0},
        "realizedPnl": pnl,
        "openedAt": "2026-07-15T10:00:00",
        "closedAt": "2026-07-25T14:00:00",
        "thesis": {"text": "test", "invalidation": "stop at 9"},
        "stopLoss": 9.0,
    }
    store.write_trade(data_dir, trade)
    store.append_event(data_dir, {"ts": "2026-07-15T10:00:00", "kind": "open", "tradeId": trade_id, "payload": {}})
    store.append_event(data_dir, {"ts": "2026-07-15T10:01:00", "kind": "fill", "tradeId": trade_id, "payload": {"qty": 100, "price": 10.0}})
    store.append_event(data_dir, {"ts": "2026-07-25T14:00:00", "kind": "close", "tradeId": trade_id, "payload": {"qty": 100, "price": 11.0}})


def test_cycle_audit_returns_observation_level_for_small_sample(tmp_path: Path):
    """< 10 笔 → observation level, 不提案。"""
    for i in range(3):
        _make_trade(tmp_path, f"00000{i}_20260801_1")

    result = run_cycle_audit(tmp_path)
    assert result["auditLevel"] == "observation"
    assert result["canPropose"] is False
    assert result["sampleSize"] == 3
    assert "样本不足" in result["note"]


def test_cycle_audit_returns_can_propose_for_10_plus(tmp_path: Path):
    """≥ 10 笔 → can_propose level。"""
    for i in range(10):
        _make_trade(tmp_path, f"00000{i:02d}_20260801_1")

    result = run_cycle_audit(tmp_path)
    assert result["auditLevel"] == "can_propose"
    assert result["canPropose"] is True
    assert result["sampleSize"] == 10


def test_cycle_audit_aggregates_red_flag_types():
    """红旗按类型聚合统计正确。"""
    flags = {
        "trade_001": [{"type": "relaxed_stop", "ts": "2026-01-01"}, {"type": "loss_add", "ts": "2026-01-02"}],
        "trade_002": [{"type": "relaxed_stop", "ts": "2026-01-03"}],
        "trade_003": [],
        "global": [{"type": "gate_proliferation"}],
    }
    result = _aggregate_red_flags(flags)
    assert result["totalFlags"] == 3
    assert result["tradesWithFlags"] == 2
    assert result["tradesWithoutFlags"] == 1
    assert result["byType"]["relaxed_stop"]["count"] == 2
    assert result["byType"]["loss_add"]["count"] == 1


def test_cycle_audit_aggregates_autopsy_distribution():
    """归因分类分布统计正确。"""
    autopsies = {
        "t1": {"classification": "A", "patternIds": [1, 3]},
        "t2": {"classification": "B", "patternIds": [1]},
        "t3": {"classification": "A", "patternIds": []},
    }
    result = _aggregate_autopsies(autopsies)
    assert result["distribution"] == {"A": 2, "B": 1, "C": 0, "D": 0}
    assert result["patternFrequency"][1] == 2
    assert result["patternFrequency"][3] == 1


def test_cycle_audit_strategy_breakdown(tmp_path: Path):
    """策略族统计正确。"""
    for i in range(5):
        _make_trade(tmp_path, f"boll_{i:02d}_20260801_1", strategy="boll_breakout", pnl=100.0)
    for i in range(3):
        _make_trade(tmp_path, f"macd_{i:02d}_20260801_1", strategy="macd_cross", pnl=-50.0)

    result = run_cycle_audit(tmp_path)
    by_strat = result["strategies"]["byStrategy"]
    assert "boll_breakout" in by_strat
    assert "macd_cross" in by_strat
    assert by_strat["boll_breakout"]["closedCount"] == 5
    assert by_strat["boll_breakout"]["realizedPnl"] == 500.0
    assert by_strat["macd_cross"]["realizedPnl"] == -150.0


def test_cycle_audit_proposal_validation_returns_empty_when_no_proposals(tmp_path: Path):
    """无提案时 proposalValidation 返回 checked=0。"""
    _make_trade(tmp_path, "test_001_20260801_1")
    result = run_cycle_audit(tmp_path)
    assert result["proposalValidation"]["checked"] == 0
