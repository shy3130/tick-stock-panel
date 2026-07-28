from __future__ import annotations

import json
from datetime import date, timedelta
from importlib import import_module

import pytest

from app.services import strategy_cache


def _paper():
    return import_module("app.services.paper_account")


def _buy(
    data_dir,
    *,
    symbol: str = "600000.SH",
    name: str = "浦发银行",
    quantity: int = 100,
    price: object = "10.00",
    trade_date: date | str = date(2026, 7, 27),
    plan_note: str = "只做模拟练习",
    invalidation_note: str = "跌破观察条件后停止跟踪",
):
    return _paper().record_trade(
        data_dir,
        symbol=symbol,
        name=name,
        side="BUY",
        quantity=quantity,
        price=price,
        trade_date=trade_date,
        plan_note=plan_note,
        invalidation_note=invalidation_note,
    )


def test_lazy_initialization_creates_versioned_default_account(tmp_path):
    account = _paper().get_account(tmp_path, as_of=date(2026, 7, 29))

    path = tmp_path / "user_data" / "paper_account.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 1
    assert account["initial_cash"] == 10_000.0
    assert account["cash"] == 10_000.0
    assert account["cost_basis"] == 0.0
    assert account["marked_value"] == 0.0
    assert account["market_value"] == 0.0
    assert account["total_equity"] == 10_000.0
    assert account["realized_pnl"] == 0.0
    assert account["unrealized_pnl"] == 0.0
    assert account["total_pnl"] == 0.0
    assert account["positions"] == []
    assert account["journal"] == []
    assert account["valuation_warnings"] == []
    assert account["fee_assumptions"] == {
        "commission_rate": 0.0003,
        "commission_rate_label": "0.03%",
        "minimum_commission": 5.0,
        "sell_stamp_tax_rate": 0.0005,
        "sell_stamp_tax_rate_label": "0.05%",
        "slippage": "用户填写的模拟成交价视为已经包含自行判断的滑点",
        "disclaimer": "费用仅为模拟假设, 实际费用以券商为准",
    }


@pytest.mark.parametrize("initial_cash", [5_000, 10_000])
def test_reset_accepts_only_the_two_supported_initial_cash_values(tmp_path, initial_cash):
    _buy(tmp_path)

    account = _paper().reset_account(
        tmp_path,
        initial_cash=initial_cash,
        confirmation="RESET",
        as_of=date(2026, 7, 29),
    )

    assert account["initial_cash"] == float(initial_cash)
    assert account["cash"] == float(initial_cash)
    assert account["positions"] == []
    assert account["journal"] == []


@pytest.mark.parametrize(
    ("initial_cash", "confirmation", "message"),
    [
        (8_000, "RESET", "初始资金"),
        (5_000, "reset", "RESET"),
        (10_000, "", "RESET"),
    ],
)
def test_reset_rejects_bad_value_or_confirmation(
    tmp_path,
    initial_cash,
    confirmation,
    message,
):
    paper = _paper()

    with pytest.raises(paper.PaperAccountValidationError, match=message):
        paper.reset_account(
            tmp_path,
            initial_cash=initial_cash,
            confirmation=confirmation,
        )


def test_fee_boundaries_apply_minimum_commission_and_sell_stamp_tax():
    paper = _paper()

    small = paper.calculate_trade_fees(price="10.00", quantity=100, side="BUY")
    large = paper.calculate_trade_fees(price="100.00", quantity=1000, side="SELL")

    assert small == {
        "gross_amount": 1000.0,
        "commission": 5.0,
        "stamp_tax": 0.0,
        "total_fees": 5.0,
    }
    assert large == {
        "gross_amount": 100_000.0,
        "commission": 30.0,
        "stamp_tax": 50.0,
        "total_fees": 80.0,
    }


@pytest.mark.parametrize(
    ("symbol", "quantity", "message"),
    [
        ("600000.SH", 50, "100 股"),
        ("688001.SH", 100, "至少 200 股"),
        ("688001.SH", 250, "100 股"),
        ("510300.SH", 100, "仅支持"),
        ("113001.SH", 100, "仅支持"),
        ("AAPL.US", 100, "仅支持"),
    ],
)
def test_buy_enforces_stock_universe_and_board_lot_rules(
    tmp_path,
    symbol,
    quantity,
    message,
):
    paper = _paper()

    with pytest.raises(paper.PaperAccountValidationError, match=message):
        _buy(tmp_path, symbol=symbol, quantity=quantity)


def test_star_market_buy_accepts_200_shares_then_100_share_increments(tmp_path):
    account = _buy(
        tmp_path,
        symbol="688001.SH",
        name="华兴源创",
        quantity=300,
        price="10.00",
    )

    assert account["positions"][0]["quantity"] == 300
    assert account["cash"] == 6_995.0


def test_buy_rejects_insufficient_cash_after_commission(tmp_path):
    paper = _paper()

    with pytest.raises(paper.PaperAccountValidationError, match="现金不足"):
        _buy(tmp_path, quantity=1000, price="10.00")


@pytest.mark.parametrize(
    "bad_price",
    [0, -1, "NaN", "Infinity", "1e999999", object()],
)
def test_trade_rejects_non_positive_or_non_finite_price(tmp_path, bad_price):
    paper = _paper()

    with pytest.raises(paper.PaperAccountValidationError, match="成交价"):
        _buy(tmp_path, price=bad_price)


def test_fifo_sale_reconciles_realized_profit_and_all_fees(tmp_path):
    paper = _paper()
    _buy(tmp_path, price="10.00", trade_date="2026-07-27")
    _buy(tmp_path, price="12.00", trade_date="2026-07-28")

    account = paper.record_trade(
        tmp_path,
        symbol="600000.SH",
        name="浦发银行",
        side="SELL",
        quantity=100,
        price="15.00",
        trade_date="2026-07-29",
        plan_note="按原计划模拟退出",
        invalidation_note="观察条件已失效",
    )

    assert account["cash"] == 9_284.25
    assert account["realized_pnl"] == 489.25
    assert account["cost_basis"] == 1_205.0
    assert account["marked_value"] == 1_205.0
    assert account["unrealized_pnl"] == 0.0
    assert account["total_pnl"] == 489.25
    assert account["total_equity"] == 10_489.25
    assert account["positions"] == [
        {
            "symbol": "600000.SH",
            "name": "浦发银行",
            "quantity": 100,
            "sellable_quantity": 100,
            "average_cost": 12.05,
            "cost_basis": 1205.0,
            "mark_price": 12.05,
            "marked_value": 1205.0,
            "market_value": 1205.0,
            "unrealized_pnl": 0.0,
            "mark_source": "COST_FALLBACK",
        }
    ]
    sale = account["journal"][-1]
    assert sale["commission"] == 5.0
    assert sale["stamp_tax"] == 0.75
    assert sale["total_fees"] == 5.75
    assert sale["realized_pnl"] == 489.25


def test_t_plus_one_blocks_same_day_sale_then_allows_next_day(tmp_path):
    paper = _paper()
    _buy(tmp_path, trade_date="2026-07-29")

    with pytest.raises(paper.PaperAccountValidationError, match=r"T\+1"):
        paper.record_trade(
            tmp_path,
            symbol="600000.SH",
            name="浦发银行",
            side="SELL",
            quantity=100,
            price="11.00",
            trade_date="2026-07-29",
            plan_note="同日模拟退出",
            invalidation_note="无",
        )

    account = paper.record_trade(
        tmp_path,
        symbol="600000.SH",
        name="浦发银行",
        side="SELL",
        quantity=100,
        price="11.00",
        trade_date="2026-07-30",
        plan_note="次日模拟退出",
        invalidation_note="无",
    )
    assert account["positions"] == []


def test_sell_rejects_short_sale_and_quantity_above_position(tmp_path):
    paper = _paper()
    with pytest.raises(paper.PaperAccountValidationError, match="没有可模拟卖出的持仓"):
        paper.record_trade(
            tmp_path,
            symbol="600000.SH",
            name="浦发银行",
            side="SELL",
            quantity=100,
            price="10.00",
            trade_date="2026-07-29",
            plan_note="",
            invalidation_note="",
        )

    _buy(tmp_path, trade_date="2026-07-27")
    with pytest.raises(paper.PaperAccountValidationError, match="超过持仓"):
        paper.record_trade(
            tmp_path,
            symbol="600000.SH",
            name="浦发银行",
            side="SELL",
            quantity=200,
            price="10.00",
            trade_date="2026-07-29",
            plan_note="",
            invalidation_note="",
        )


def test_atomic_persistence_reloads_without_temporary_file(tmp_path):
    first = _buy(tmp_path)
    path = tmp_path / "user_data" / "paper_account.json"

    reloaded = _paper().get_account(tmp_path, as_of=date(2026, 7, 29))

    assert reloaded["cash"] == first["cash"]
    assert reloaded["positions"] == first["positions"]
    assert reloaded["journal"] == first["journal"]
    assert not path.with_suffix(".json.tmp").exists()


def test_missing_cache_uses_cost_fallback_and_emits_warning(tmp_path, monkeypatch):
    paper = _paper()
    monkeypatch.setattr(paper.strategy_cache, "read_cache", lambda data_dir: None)
    account = _buy(tmp_path)

    assert account["positions"][0]["mark_source"] == "COST_FALLBACK"
    assert account["valuation_warnings"] == [
        {
            "code": "COST_FALLBACK",
            "symbol": "600000.SH",
            "message": "策略缓存没有可用价格, 当前按持仓成本估值",
        }
    ]


def test_valid_strategy_cache_marks_position_deterministically(tmp_path):
    _buy(tmp_path)
    strategy_cache.write_cache(
        tmp_path,
        "2026-07-29",
        {
            "strategy_b": {
                "as_of": "2026-07-29",
                "total": 1,
                "rows": [{"symbol": "600000.SH", "close": 13.0}],
            },
            "strategy_a": {
                "as_of": "2026-07-29",
                "total": 1,
                "rows": [{"symbol": "600000.SH", "close": 12.34}],
            },
        },
    )

    account = _paper().get_account(tmp_path, as_of=date(2026, 7, 29))

    position = account["positions"][0]
    assert position["mark_source"] == "STRATEGY_CACHE"
    assert position["mark_price"] == 12.34
    assert position["marked_value"] == 1_234.0
    assert account["unrealized_pnl"] == 229.0
    assert account["total_pnl"] == 229.0


def test_journal_is_immutable_and_money_totals_reconcile(tmp_path):
    account = _buy(
        tmp_path,
        plan_note="原始计划",
        invalidation_note="原始失效条件",
    )
    entry = account["journal"][0]

    assert entry["id"]
    assert entry["timestamp"].endswith("+00:00")
    assert entry["plan_note"] == "原始计划"
    assert entry["invalidation_note"] == "原始失效条件"
    assert entry["cash_before"] == 10_000.0
    assert entry["cash_after"] == 8_995.0
    assert account["total_equity"] == account["cash"] + account["marked_value"]
    assert account["total_pnl"] == account["realized_pnl"] + account["unrealized_pnl"]

    entry["plan_note"] = "调用方试图篡改"
    reloaded = _paper().get_account(tmp_path, as_of=date(2026, 7, 29))
    assert reloaded["journal"][0]["plan_note"] == "原始计划"
    assert reloaded["journal"][0]["id"] == entry["id"]


def test_sellable_quantity_uses_valuation_date(tmp_path):
    today = date(2026, 7, 29)
    _buy(tmp_path, trade_date=today)

    same_day = _paper().get_account(tmp_path, as_of=today)
    next_day = _paper().get_account(tmp_path, as_of=today + timedelta(days=1))

    assert same_day["positions"][0]["sellable_quantity"] == 0
    assert next_day["positions"][0]["sellable_quantity"] == 100
