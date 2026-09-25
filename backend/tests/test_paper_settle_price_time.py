"""模拟盘盘后结算不按下单时刻之前已经打印的价格成交。

次日开盘单 = 次一交易日 raw_open 成交 (docs/paper-trading-plan.md 撮合规则表)。
settle_day(当日) 却把当日开盘之后才创建 (或涨跌停排队) 的 next_open 单按当日
09:30 的开盘价成交, 收盘后才下的 close / 即时单也按当日 15:00 的收盘价兜底成交 ——
都是拿下单时已经知道的价格回填成交。盘中自动跟单 (缺省 next_open) 因此系统性
按当日开盘价买入。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
ETF = "510300.SH"
DAY = date(2026, 9, 24)
NEXT = DAY + timedelta(days=1)
# (日期, open, close): 当日开 10.2 收 10.8; 次日开 10.5 收 10.9 (均在涨跌幅内)
BARS = [(DAY - timedelta(days=1), 10.0, 10.0), (DAY, 10.2, 10.8), (NEXT, 10.5, 10.9)]


def _frame(symbol: str, rows: list[tuple[date, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(rows),
            "date": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [max(r[1], r[2]) for r in rows],
            "low": [min(r[1], r[2]) for r in rows],
            "close": [r[2] for r in rows],
            "volume": [10000.0] * len(rows),
            "amount": [r[2] * 10000.0 for r in rows],
        }
    )


def _write_bars(tmp_path: Path) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(_frame(SYM, BARS))
    repo.append_etf_daily(_frame(ETF, BARS))


def _freeze(monkeypatch, day: date, hhmm: time) -> None:
    """把域模块的北京时钟钉在某交易日某时刻 (created_at / queued_at 由此产生)。"""
    now = datetime.combine(day, hhmm, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper, "cn_today", lambda: now.date())


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    _write_bars(tmp_path)
    return tmp_path


def test_next_open_placed_after_open_waits_for_next_trading_day(data_dir, monkeypatch):
    """14:00 下的次日开盘单: 当日结算不成交 (不计顺延), 次日按次日开盘价成交。"""
    _freeze(monkeypatch, DAY, time(14, 0))
    paper.create_account(data_dir, 1_000_000)
    order, err = paper.create_order(data_dir, SYM, "buy", qty=100, order_type="next_open", ref_price=10.8)
    assert err is None

    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 0
    got = paper.get_order(data_dir, order["id"])
    assert got["status"] == "pending" and got["postponed"] == 0

    assert paper.settle_day(data_dir, NEXT.isoformat())["filled"] == 1
    got = paper.get_order(data_dir, order["id"])
    assert got["status"] == "filled"
    assert got["fill_price"] == pytest.approx(paper.apply_slippage(10.5, "buy", 5.0))


def test_next_open_placed_before_open_fills_same_day_open(data_dir, monkeypatch):
    """开盘前 (08:00) 下的次日开盘单: 当日开盘价就是下一次开盘, 当日结算照常成交。"""
    _freeze(monkeypatch, DAY, time(8, 0))
    paper.create_account(data_dir, 1_000_000)
    order, err = paper.create_order(data_dir, SYM, "buy", qty=100, order_type="next_open", ref_price=10.0)
    assert err is None
    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 1
    got = paper.get_order(data_dir, order["id"])
    assert got["fill_price"] == pytest.approx(paper.apply_slippage(10.2, "buy", 5.0))


def test_etf_market_order_intraday_waits_for_next_open(data_dir, monkeypatch):
    """ETF 即时单盘中自动转次日开盘: 同样不能按当日已过去的开盘价成交。"""
    _freeze(monkeypatch, DAY, time(10, 30))
    paper.create_account(data_dir, 1_000_000)
    order, err = paper.create_order(data_dir, ETF, "buy", qty=100, asset_type="etf", ref_price=10.5)
    assert err is None and order["order_type"] == "next_open"
    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 0
    assert paper.settle_day(data_dir, NEXT.isoformat())["filled"] == 1
    assert paper.get_order(data_dir, order["id"])["fill_price"] == pytest.approx(paper.apply_slippage(10.5, "buy", 5.0))


def test_limit_queue_intraday_waits_for_next_open(data_dir, monkeypatch):
    """盘中触及涨停转排队 (10:00): 当日开盘价 10.2 在排队之前已打印, 当日结算不得成交。"""
    _freeze(monkeypatch, DAY, time(10, 0))
    paper.create_account(data_dir, 1_000_000, queue_limit_orders=True)
    order, err = paper.create_order(data_dir, SYM, "buy", qty=100, ref_price=10.5)
    assert err is None
    assert paper.evaluate_intraday(data_dir, {SYM: 11.0}) == []  # 涨停 11.0 → 排队
    queued = paper.get_order(data_dir, order["id"])
    assert queued["status"] == "pending" and queued["order_type"] == "next_open" and queued["postponed"] == 1

    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 0
    got = paper.get_order(data_dir, order["id"])
    assert got["status"] == "pending" and got["postponed"] == 1

    assert paper.settle_day(data_dir, NEXT.isoformat())["filled"] == 1
    assert paper.get_order(data_dir, order["id"])["fill_price"] == pytest.approx(paper.apply_slippage(10.5, "buy", 5.0))


def test_auto_rule_intraday_order_waits_for_next_open(data_dir, monkeypatch):
    """自动跟单在盘中 (13:30) 触发的次日开盘单: 不按当日开盘价成交。"""
    _freeze(monkeypatch, DAY, time(13, 30))
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, {
        "name": "跟策略", "match_kind": "strategy", "match_id": "strat_1",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 5000,
        "order_type": "next_open", "cooldown_days": 5,
    })
    created = paper_auto.on_rule_events(data_dir, [
        {"source": "strategy", "strategy_id": "strat_1", "symbol": SYM, "price": 10.6},
    ])
    assert len(created) == 1 and created[0]["order_type"] == "next_open"

    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 0
    assert paper.settle_day(data_dir, NEXT.isoformat())["filled"] == 1
    got = paper.get_order(data_dir, created[0]["id"])
    assert got["fill_price"] == pytest.approx(paper.apply_slippage(10.5, "buy", 5.0))


def test_close_order_placed_after_close_waits_for_next_close(data_dir, monkeypatch):
    """收盘后 (15:05) 下的收盘单: 当日收盘价已知, 留到次日按次日收盘价成交。"""
    _freeze(monkeypatch, DAY, time(15, 5))
    paper.create_account(data_dir, 1_000_000)
    order, err = paper.create_order(data_dir, SYM, "buy", qty=100, order_type="close", ref_price=10.8)
    assert err is None
    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 0
    assert paper.settle_day(data_dir, NEXT.isoformat())["filled"] == 1
    assert paper.get_order(data_dir, order["id"])["fill_price"] == pytest.approx(paper.apply_slippage(10.9, "buy", 5.0))


def test_close_order_placed_in_session_fills_same_day_close(data_dir, monkeypatch):
    """盘中 (14:00) 下的收盘单照常按当日收盘价成交。"""
    _freeze(monkeypatch, DAY, time(14, 0))
    paper.create_account(data_dir, 1_000_000)
    order, err = paper.create_order(data_dir, SYM, "buy", qty=100, order_type="close", ref_price=10.8)
    assert err is None
    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 1
    assert paper.get_order(data_dir, order["id"])["fill_price"] == pytest.approx(paper.apply_slippage(10.8, "buy", 5.0))


def test_market_order_placed_after_close_not_filled_by_close_fallback(data_dir, monkeypatch):
    """收盘后 (15:05) 下的即时单: 当日结算不按收盘价兜底, 留到次日盘中按快照成交。"""
    _freeze(monkeypatch, DAY, time(15, 5))
    paper.create_account(data_dir, 1_000_000)
    order, err = paper.create_order(data_dir, SYM, "buy", qty=100, ref_price=10.8)
    assert err is None
    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 0
    assert paper.get_order(data_dir, order["id"])["status"] == "pending"

    _freeze(monkeypatch, NEXT, time(9, 35))
    assert len(paper.evaluate_intraday(data_dir, {SYM: 10.6})) == 1
    assert paper.get_order(data_dir, order["id"])["fill_price"] == pytest.approx(paper.apply_slippage(10.6, "buy", 5.0))


def test_naive_stamp_is_beijing_and_unparseable_stamp_keeps_old_behavior(data_dir, monkeypatch):
    """兼容: 无时区的时间戳按北京墙钟判定; 时间戳缺失/损坏的旧订单不拦截 (沿用原行为)。"""
    _freeze(monkeypatch, DAY, time(14, 0))
    paper.create_account(data_dir, 1_000_000)
    naive, _ = paper.create_order(data_dir, SYM, "buy", qty=100, order_type="next_open", ref_price=10.8)
    broken, _ = paper.create_order(data_dir, SYM, "buy", qty=100, order_type="next_open", ref_price=10.8)
    naive["created_at"] = f"{DAY.isoformat()}T14:00:00"
    paper.save_order(data_dir, naive)
    broken["created_at"] = ""
    paper.save_order(data_dir, broken)

    assert paper.settle_day(data_dir, DAY.isoformat())["filled"] == 1
    assert paper.get_order(data_dir, naive["id"])["status"] == "pending"
    assert paper.get_order(data_dir, broken["id"])["status"] == "filled"
