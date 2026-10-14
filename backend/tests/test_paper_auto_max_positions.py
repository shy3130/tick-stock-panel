"""自动跟单 max_positions (最大持仓数) 语义测试 — 对齐回测引擎口径。

约束只在买入开新仓时生效:
- 已占槽 (当前持仓 + 待成交买单) 达上限后, 新标的买入信号被跳过
- 已持仓标的加仓不受限; 卖出信号不受限
- max_positions = 0 (默认) 不限制
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto

SYM = "600519.SH"
SYM2 = "000001.SZ"
DAY = date(2026, 9, 24)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(autouse=True)
def _market_session(monkeypatch):
    """钉在连续竞价时段: 即时单按事件价直接成交。"""
    now = datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper, "cn_today", lambda: now.date())


def _rule(**over) -> dict:
    base = {
        "name": "跟策略", "match_kind": "strategy", "match_id": "strat_1",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10000,
        "order_type": "market", "cooldown_days": 0,
    }
    base.update(over)
    return base


def _ev(symbol: str, price: float = 10.0) -> dict:
    return {"source": "strategy", "strategy_id": "strat_1", "symbol": symbol, "price": price}


def test_max_positions_blocks_new_symbol(data_dir: Path):
    """上限 1: 第一标的信号开仓 (pending 占槽) 后, 第二标的的买入信号被跳过。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_positions=1))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM), _ev(SYM2, 12.0)])
    assert len(created) == 1 and created[0]["symbol"] == SYM

    # 钩子撮合后: 只持有 SYM
    paper.evaluate_intraday(data_dir, {SYM: 10.0, SYM2: 12.0})
    positions = paper.load_positions(data_dir)
    assert positions[SYM]["qty"] > 0
    assert SYM2 not in positions or positions[SYM2]["qty"] == 0


def test_max_positions_allows_rebuy_held_symbol(data_dir: Path):
    """上限 1: 已持仓标的再加仓不受限 (回测语义: 加仓不占新槽)。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_positions=1))

    assert len(paper_auto.on_rule_events(data_dir, [_ev(SYM)])) == 1
    paper.evaluate_intraday(data_dir, {SYM: 10.0})          # 成交建仓 (1万/10元 = 1000股)
    assert paper.load_positions(data_dir)[SYM]["qty"] == 1000
    assert len(paper_auto.on_rule_events(data_dir, [_ev(SYM)])) == 1  # 加仓通过
    paper.evaluate_intraday(data_dir, {SYM: 10.0})
    assert paper.load_positions(data_dir)[SYM]["qty"] == 2000


def test_max_positions_pending_buy_occupies_slot(data_dir: Path):
    """待成交买单占槽: next_open 单未成交时, 新标的信号同样被跳过。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_positions=1, order_type="next_open"))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM), _ev(SYM2, 12.0)])
    # 同批: 第一单 pending 占槽 → 第二标的跳过
    assert len(created) == 1 and created[0]["order_type"] == "next_open"
    assert paper.get_order(data_dir, created[0]["id"])["status"] == "pending"


def test_max_positions_zero_means_unlimited(data_dir: Path):
    """0 (默认) 不限制: 多标的照常开仓。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_positions=0))

    assert len(paper_auto.on_rule_events(data_dir, [_ev(SYM), _ev(SYM2, 12.0)])) == 2


def test_max_positions_does_not_limit_sell(data_dir: Path, monkeypatch):
    """卖出方向不受 max_positions 约束 (T+1: 次一交易日才可卖)。"""
    paper.create_account(data_dir, 1_000_000)
    # 先建仓两标的各 1000 股 (即时单 + 钩子撮合; 卖出按金额 1万/价10 = 1000 股)
    for sym in (SYM, SYM2):
        order, err = paper.create_order(data_dir, sym, "buy", qty=1000, ref_price=10.0)
        assert err is None
    paper.evaluate_intraday(data_dir, {SYM: 10.0, SYM2: 10.0})
    # 推进到次一交易日, 绕开 T+1 可卖限制
    next_day = datetime.combine(DAY + timedelta(days=1), time(10, 0), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: next_day)
    monkeypatch.setattr(paper, "cn_today", lambda: next_day.date())
    paper_auto.create_auto_rule(data_dir, _rule(side="sell", max_positions=1))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM), _ev(SYM2, 10.0)])
    assert len(created) == 2 and all(o["side"] == "sell" for o in created)


def test_rule_validation_rejects_bad_max_positions(tmp_path: Path):
    with pytest.raises(ValueError, match="max_positions"):
        paper_auto.create_auto_rule(tmp_path, _rule(max_positions=-1))
    with pytest.raises(ValueError, match="max_positions"):
        paper_auto.create_auto_rule(tmp_path, _rule(max_positions=1.5))
    # 旧规则文件缺字段 → normalize 补 0 (不限), 不破坏存量
    rule = paper_auto.normalize_rule(_rule())
    assert rule["max_positions"] == 0


def _ev_typed(etype: str, symbol: str, price: float = 10.0) -> dict:
    return {"source": "strategy", "strategy_id": "strat_1", "type": etype, "symbol": symbol, "price": price}


def test_auto_side_follows_signal_direction(data_dir: Path, monkeypatch):
    """side=auto: 入场信号买入, 出场信号全平 — 一条规则表达完整策略周期。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(side="auto"))

    # 入场信号 → 买入
    created = paper_auto.on_rule_events(data_dir, [_ev_typed("buy_signal", SYM)])
    assert len(created) == 1 and created[0]["side"] == "buy"
    paper.evaluate_intraday(data_dir, {SYM: 10.0})
    assert paper.load_positions(data_dir)[SYM]["qty"] == 1000

    # 次日出场信号 → 全平 (T+1)
    next_day = datetime.combine(DAY + timedelta(days=1), time(10, 0), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: next_day)
    monkeypatch.setattr(paper, "cn_today", lambda: next_day.date())
    created = paper_auto.on_rule_events(data_dir, [_ev_typed("sell_signal", SYM)])
    assert len(created) == 1 and created[0]["side"] == "sell" and created[0]["qty"] == 1000

    # 未持仓时出场信号 → 跳过
    paper.evaluate_intraday(data_dir, {SYM: 10.0})
    assert paper_auto.on_rule_events(data_dir, [_ev_typed("pool_exit", SYM)]) == []


def test_auto_side_pool_entry_exit(data_dir: Path, monkeypatch):
    """auto 同样认进池/移出池事件 (结果池口径的策略)。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(side="auto"))

    assert len(paper_auto.on_rule_events(data_dir, [_ev_typed("pool_entry", SYM)])) == 1
    paper.evaluate_intraday(data_dir, {SYM: 10.0})
    next_day = datetime.combine(DAY + timedelta(days=1), time(10, 0), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: next_day)
    monkeypatch.setattr(paper, "cn_today", lambda: next_day.date())
    created = paper_auto.on_rule_events(data_dir, [_ev_typed("pool_exit", SYM)])
    assert len(created) == 1 and created[0]["qty"] == 1000


def test_auto_side_rejects_non_strategy_match(tmp_path: Path):
    """auto 仅限匹配策略 (监控规则事件无方向语义)。"""
    with pytest.raises(ValueError, match="auto"):
        paper_auto.create_auto_rule(tmp_path, _rule(side="auto", match_kind="rule"))
    # normalize: strategy 缺 side → auto; rule 缺 side → buy (存量兼容)
    no_side = _rule(); no_side.pop("side")
    assert paper_auto.normalize_rule(no_side)["side"] == "auto"
    no_side_r = _rule(match_kind="rule"); no_side_r.pop("side")
    assert paper_auto.normalize_rule(no_side_r)["side"] == "buy"
