"""date 规则类型 (日期提醒) 与批次登记 (lot → 自动生成监控规则) 的纯逻辑测试。

不碰网络/行情; 窗口判断与引擎 cooldown 用相对今天的日期保证确定性。
"""
from __future__ import annotations

import time
from datetime import timedelta

import polars as pl
import pytest

from app.api import lots
from app.market_time import cn_today
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine
from app.strategy.monitor_rules import date_rule_in_window


# ── date_rule_in_window 窗口边界 ─────────────────────────
def test_window_boundaries():
    # 窗口 [08-17, 08-20], lead=3
    assert not date_rule_in_window("2026-08-20", 3, "2026-08-16")  # 窗口前
    assert date_rule_in_window("2026-08-20", 3, "2026-08-17")      # 窗口起点
    assert date_rule_in_window("2026-08-20", 3, "2026-08-20")      # 末日
    assert not date_rule_in_window("2026-08-20", 3, "2026-08-21")  # 窗口后


def test_window_lead_zero():
    assert not date_rule_in_window("2026-08-20", 0, "2026-08-19")
    assert date_rule_in_window("2026-08-20", 0, "2026-08-20")


def test_window_invalid_inputs():
    assert not date_rule_in_window("", 3, "2026-08-18")
    assert not date_rule_in_window(None, 3, "2026-08-18")  # type: ignore[arg-type]
    assert not date_rule_in_window("2026-13-40", 3, "2026-08-18")
    assert not date_rule_in_window("2026-08-20", -2, "2026-08-18")  # 负提前按 0 处理


# ── validate: date 分支 ─────────────────────────────────
def _date_rule(**overrides) -> dict:
    return {
        "id": "mr_d1",
        "name": "日期提醒",
        "type": "date",
        "scope": "symbols",
        "symbols": [],
        "remind_date": "2026-08-20",
        "lead_days": 2,
        **overrides,
    }


def test_validate_date_ok():
    monitor_rules.validate(_date_rule())  # 空 symbols 应通过 (与 price 不同)


def test_validate_date_missing_remind():
    with pytest.raises(ValueError, match="remind_date"):
        monitor_rules.validate(_date_rule(remind_date=""))
    with pytest.raises(ValueError, match="remind_date"):
        monitor_rules.validate(_date_rule(remind_date=None))


def test_validate_date_bad_remind():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        monitor_rules.validate(_date_rule(remind_date="2026/08/20"))


def test_validate_date_bad_lead():
    with pytest.raises(ValueError, match="lead_days"):
        monitor_rules.validate(_date_rule(lead_days=-1))


def test_validate_date_rejects_conditions():
    with pytest.raises(ValueError, match="conditions"):
        monitor_rules.validate(_date_rule(conditions=[{"field": "close", "op": "<", "value": 10}]))


# ── normalize: date 默认值 ──────────────────────────────
def test_normalize_date_defaults():
    r = monitor_rules.normalize({"id": "mr_d2", "name": "n", "type": "date"})
    assert r["conditions"] == []
    assert r["cooldown_seconds"] == 86400  # 每天最多一次
    assert r["lead_days"] == 1             # 缺省提前 1 天
    assert r["remind_date"] is None


def test_normalize_date_preserves_explicit_zero_lead():
    r = monitor_rules.normalize({"id": "mr_d3", "name": "n", "type": "date", "lead_days": 0})
    assert r["lead_days"] == 0  # 显式 0 不强制成 1


def test_normalize_preserves_unknown_keys():
    r = monitor_rules.normalize({"id": "mr_d4", "name": "n", "type": "date", "lot_id": "lot_x"})
    assert r["lot_id"] == "lot_x"  # 批次托管标记不丢


# ── 引擎: evaluate 跳过 date / evaluate_date_rules ──────
def test_evaluate_skips_date_rules():
    eng = MonitorRuleEngine()
    eng.set_rules([monitor_rules.normalize(_date_rule())])
    df = pl.DataFrame({"symbol": ["600519.SH"], "close": [1500.0]})
    assert eng.evaluate(df, asset_type="stock") == []


def test_evaluate_date_rules_fires_once_per_day():
    eng = MonitorRuleEngine()
    rule = monitor_rules.normalize(_date_rule(
        remind_date=(cn_today() + timedelta(days=1)).isoformat(), lead_days=2,
        symbols=["600519.SH"],
    ))
    eng.set_rules([rule])
    now = time.time()
    evs = eng.evaluate_date_rules(now=now)
    assert len(evs) == 1
    assert evs[0]["source"] == "date"
    assert evs[0]["type"] == "date_reminder"
    assert evs[0]["rule_id"] == rule["id"]
    assert evs[0]["symbol"] == "600519.SH"
    # 当天第二次评估: cooldown 按天隔离, 不再触发
    assert eng.evaluate_date_rules(now=now + 30) == []


def test_evaluate_date_rules_after_remind_date_stops():
    eng = MonitorRuleEngine()
    eng.set_rules([monitor_rules.normalize(_date_rule(
        remind_date=(cn_today() - timedelta(days=1)).isoformat(), lead_days=0,
    ))])
    assert eng.evaluate_date_rules(now=time.time()) == []


def test_evaluate_date_rules_respects_enabled():
    eng = MonitorRuleEngine()
    eng.set_rules([monitor_rules.normalize(_date_rule(
        remind_date=(cn_today() + timedelta(days=1)).isoformat(), lead_days=2, enabled=False,
    ))])
    assert eng.evaluate_date_rules(now=time.time()) == []


# ── 批次 → 规则 (lot_to_rules 纯映射) ────────────────────
def _lot(**overrides) -> dict:
    return {
        "id": "lot_abc",
        "symbol": "600519.SH",
        "qty": 100,
        "cost_price": 1500.0,
        "target_pct": 10,
        "stop_pct": 5,
        "remind_date": "2026-08-20",
        "lead_days": 2,
        **overrides,
    }


def test_lot_to_rules_both():
    price, date_rule = lots.lot_to_rules(_lot())
    assert price is not None and date_rule is not None
    # 1500×1.1=1650, 1500×0.95=1425
    assert price["conditions"] == [
        {"field": "close", "op": ">=", "value": 1650.0},
        {"field": "close", "op": "<=", "value": 1425.0},
    ]
    assert price["logic"] == "or"
    assert price["id"] == "lot_abc_p"
    assert price["symbols"] == ["600519.SH"]
    assert price["cooldown_seconds"] == 86400
    # message: 成本 + 止盈/止损 + 股数 + 触发条件摘要 (用户得知道卖多少/触发在哪个价位)
    assert "100股" in price["message"]
    assert "收盘价>=1650.0" in price["message"] and "收盘价<=1425.0" in price["message"]
    assert date_rule["id"] == "lot_abc_d"
    assert date_rule["remind_date"] == "2026-08-20"
    assert date_rule["lead_days"] == 2
    assert "100股" in date_rule["message"]


def test_lot_to_rules_target_only():
    price, date_rule = lots.lot_to_rules(_lot(stop_pct=0, remind_date=None))
    assert price is not None and date_rule is None
    assert len(price["conditions"]) == 1
    assert price["conditions"][0] == {"field": "close", "op": ">=", "value": 1650.0}


def test_lot_to_rules_stop_only():
    price, _ = lots.lot_to_rules(_lot(target_pct=0, remind_date=None))
    assert price is not None
    assert len(price["conditions"]) == 1
    assert price["conditions"][0] == {"field": "close", "op": "<=", "value": 1425.0}


def test_lot_to_rules_nothing_to_alert():
    price, date_rule = lots.lot_to_rules(_lot(target_pct=0, stop_pct=0, remind_date=None))
    assert price is None and date_rule is None


# ── format_alert_quote (推送正文尾部) ────────────────────
def test_format_alert_quote():
    from app.strategy.monitor import format_alert_quote
    assert format_alert_quote(1650.0, 0.10) == "现价 1650.0 · +10.0%"
    assert format_alert_quote(1425.0, -0.05) == "现价 1425.0 · -5.0%"
    assert format_alert_quote(1650.0, None) == "现价 1650.0"
    assert format_alert_quote(None, 0.03) == "+3.0%"
    assert format_alert_quote(None, None) == ""


# ── validate_lot ────────────────────────────────────────
def test_validate_lot_ok():
    lots.validate_lot(_lot())


def test_validate_lot_missing_symbol():
    with pytest.raises(ValueError, match="symbol"):
        lots.validate_lot(_lot(symbol=""))


def test_validate_lot_bad_cost():
    with pytest.raises(ValueError, match="cost_price"):
        lots.validate_lot(_lot(cost_price=0))


def test_validate_lot_requires_monitor_point():
    with pytest.raises(ValueError, match="至少设置一项"):
        lots.validate_lot(_lot(target_pct=0, stop_pct=0, remind_date=None))
