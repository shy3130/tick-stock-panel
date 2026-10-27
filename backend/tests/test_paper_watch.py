"""模拟盘「绑定即评估」— paper_watch 同步与合成规则事件流端到端。

覆盖: watched 聚合 (多账户/禁用排除/非 strategy 排除); sync 注入/幂等/与真
监控规则去重/策略已删跳过/解绑移除; 合成规则产生的事件带 paper_watch 前缀
并能驱动 paper_auto 自动下单 (两轮 evaluate: 首轮基线不刷事件)。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.strategy import paper, paper_auto, paper_watch
from app.strategy.engine import StrategyResult
from app.strategy.monitor import MonitorRuleEngine


class _FakeStrategyEngine:
    """只认 demo 策略; run 按序吐结果 (与 tests/test_strategy_monitor_events 同模式)。"""

    def __init__(self, results: list[StrategyResult], known: set[str] | None = None):
        self.results = list(results)
        self.known = known if known is not None else {"demo"}
        self.strategy = SimpleNamespace(
            meta={"id": "demo", "name": "示例策略"},
            execution_backend="polars_expr",
            filter_history_fn=None,
        )

    def get(self, strategy_id: str):
        if strategy_id not in self.known:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return self.strategy

    def run(self, strategy_id: str, context, **kwargs):
        return self.results.pop(0)


def _result(pool: tuple[str, ...], buys: tuple[str, ...] = ()) -> StrategyResult:
    return StrategyResult(
        as_of=date(2026, 10, 27),
        strategy_id="demo",
        rows=[{"symbol": s, "close": 10.0, "change_pct": 0.01} for s in pool],
        total=len(pool),
        scores={},
        entry_signal_hits=[{"symbol": s, "signals": ["signal_buy"]} for s in buys],
        exit_signal_hits=[],
    )


def _quotes():
    import polars as pl

    return pl.DataFrame({
        "symbol": ["A", "B"],
        "close": [10.0, 20.0],
        "change_pct": [0.01, -0.02],
    })


@pytest.fixture()
def paper_env(tmp_path):
    """两个账户: acc1 绑 demo (启用), acc2 绑 demo (禁用) + 一条 rule 型规则。"""
    paper.create_account(tmp_path, 100_000.0, account_id="acc1", name="一")
    paper.create_account(tmp_path, 100_000.0, account_id="acc2", name="二")
    paper_auto.create_auto_rule(tmp_path, {
        "name": "跟demo", "match_kind": "strategy", "match_id": "demo",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 5000,
        "order_type": "next_open", "cooldown_days": 5, "max_positions": 0,
        "enabled": True,
    }, account_id="acc1")
    disabled = paper_auto.create_auto_rule(tmp_path, {
        "name": "停用", "match_kind": "strategy", "match_id": "demo",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 5000,
        "order_type": "next_open", "cooldown_days": 5, "max_positions": 0,
        "enabled": False,
    }, account_id="acc2")
    rule_kind = paper_auto.create_auto_rule(tmp_path, {
        "name": "跟规则", "match_kind": "rule", "match_id": "mr_x",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 5000,
        "order_type": "next_open", "cooldown_days": 5, "max_positions": 0,
        "enabled": True,
    }, account_id="acc2")
    return {"disabled_id": disabled["id"], "rule_kind_id": rule_kind["id"]}


def test_watched_strategy_ids_aggregates(tmp_path, paper_env):
    assert paper_watch.watched_strategy_ids(tmp_path) == {"demo"}


def test_sync_injects_synthetic_rule_and_is_idempotent(tmp_path, paper_env):
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_FakeStrategyEngine([]))
    result = paper_watch.sync(tmp_path, engine, _FakeStrategyEngine([]))
    assert result["added"] == ["demo"]
    rule = engine.rules["paper_watch:demo"]
    assert rule["type"] == "strategy"
    assert rule["scope"] == "all"
    assert set(rule["notify_events"]) == {"buy_signal", "sell_signal", "pool_entry", "pool_exit"}
    assert rule["enabled"] is True

    again = paper_watch.sync(tmp_path, engine, _FakeStrategyEngine([]))
    assert again["added"] == [] and again["removed"] == []


def test_sync_dedupes_against_real_monitor_rule(tmp_path, paper_env):
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_FakeStrategyEngine([]))
    engine.add_rule({
        "id": "mr_real", "name": "真监控", "type": "strategy", "strategy_id": "demo",
        "asset_type": "stock", "scope": "all", "enabled": True,
        "notify_events": ["pool_entry"],
    })
    result = paper_watch.sync(tmp_path, engine, _FakeStrategyEngine([]))
    assert result["watching"] == []
    assert "paper_watch:demo" not in engine.rules


def test_sync_skips_deleted_strategy(tmp_path, paper_env):
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_FakeStrategyEngine([], known=set()))
    result = paper_watch.sync(tmp_path, engine, _FakeStrategyEngine([], known=set()))
    assert result["watching"] == []


def test_sync_removes_unbound(tmp_path, paper_env):
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_FakeStrategyEngine([]))
    paper_watch.sync(tmp_path, engine, _FakeStrategyEngine([]))
    assert "paper_watch:demo" in engine.rules
    paper_auto.delete_auto_rule(tmp_path, _first_rule_id(tmp_path, "acc1"), account_id="acc1")
    result = paper_watch.sync(tmp_path, engine, _FakeStrategyEngine([]))
    assert result["removed"] == ["demo"]
    assert "paper_watch:demo" not in engine.rules


def _first_rule_id(data_dir, account_id) -> str:
    return paper_auto.load_auto_rules(data_dir, account_id)[0]["id"]


def test_synthetic_rule_events_drive_paper_orders(tmp_path, paper_env, monkeypatch):
    """端到端: sync 注入 → 两轮 evaluate (首轮基线) → 事件带 paper_watch 前缀 →
    paper_auto 按绑定规则建买单; 同时验证公开事件流里看不到沙盒信号。"""
    strat = _FakeStrategyEngine([
        _result(pool=("A",)),
        _result(pool=("A", "B"), buys=("B",)),
    ])
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strat)
    paper_watch.sync(tmp_path, engine, strat)
    assert engine.rule_count == 1

    import app.strategy.monitor as monitor_mod

    monkeypatch.setattr(monitor_mod.time, "time", lambda: 100.0)
    assert engine.evaluate(_quotes()) == []  # 首轮基线, 不刷事件

    events = engine.evaluate(_quotes())
    assert events, "第二轮应产生事件"
    assert all(paper_watch.is_paper_watch_event(ev) for ev in events)
    buys = [ev for ev in events if ev["type"] == "buy_signal"]
    assert {ev["symbol"] for ev in buys} == {"B"}

    created = paper_auto.on_rule_events(tmp_path, events, account_id="acc1")
    assert len(created) == 1
    order = created[0]
    assert order["symbol"] == "B"
    assert order["side"] == "buy"
    assert order["status"] == "pending"  # next_open 次日开盘成交
    # 账户二无启用规则, 不会重复下单
    assert paper_auto.on_rule_events(tmp_path, events, account_id="acc2") == []
