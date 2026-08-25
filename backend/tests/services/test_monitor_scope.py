"""监控规则 scope 安全测试 — 板块/未知作用域 fail-closed。

收口全面界面审计发现的安全问题:
1. validate 明确拒绝保存 scope=sector (中文信息须说明未提供精确板块过滤)。
2. MonitorRuleEngine 对历史已保存的 sector 规则 fail-closed 返回空 df,
   绝不退化为全市场; 未知 scope 同样 fail-closed。
3. scope=all / scope=symbols 保持原有行为不变。
4. /api/monitor-rules/options 不再向新规则提供 sector。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import monitor_rules as monitor_rules_api
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


# ── 公共构造 ───────────────────────────────────────────
def _valid_rule(**overrides) -> dict:
    """一条除 scope 外均合法的 signal 规则。"""
    r = {
        "id": "test_rule",
        "name": "测试规则",
        "type": "signal",
        "scope": "symbols",
        "symbols": ["600519.SH"],
        "conditions": [{"field": "signal_golden", "op": "truth"}],
        "logic": "and",
        "cooldown_seconds": 0,
        "severity": "info",
    }
    r.update(overrides)
    return r


def _df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["600519.SH", "000001.SZ", "600000.SH"],
            "name": ["贵州茅台", "平安银行", "浦发银行"],
            "close": [1700.0, 12.0, 10.0],
            "change_pct": [3.5, -1.0, 2.0],
        }
    )


# ── validate: 拒绝 sector / 未知 scope ─────────────────
def test_validate_rejects_sector_scope():
    rule = _valid_rule(scope="sector")
    rule.pop("symbols", None)  # sector 不需要 symbols
    with pytest.raises(ValueError) as ei:
        monitor_rules.validate(rule)
    msg = str(ei.value)
    assert "sector" in msg
    # 中文信息须说明当前未提供精确板块过滤
    assert "精确板块过滤" in msg


def test_validate_rejects_unknown_scope():
    rule = _valid_rule(scope="galaxy")
    with pytest.raises(ValueError) as ei:
        monitor_rules.validate(rule)
    assert "scope" in str(ei.value)


def test_validate_accepts_all_and_symbols():
    # 原有合法 scope 不受影响
    monitor_rules.validate(_valid_rule(scope="all"))
    monitor_rules.validate(_valid_rule(scope="symbols"))


# ── engine _apply_scope: fail-closed (实例方法: watchlist_group 需引擎 data_dir) ──
def test_apply_scope_sector_returns_empty():
    rule = {"id": "r_sector", "scope": "sector"}
    out = MonitorRuleEngine()._apply_scope(_df(), rule)
    assert out.is_empty()
    assert out.height == 0


def test_apply_scope_unknown_scope_returns_empty():
    rule = {"id": "r_x", "scope": "galaxy"}
    out = MonitorRuleEngine()._apply_scope(_df(), rule)
    assert out.is_empty()


def test_apply_scope_all_returns_full_market():
    df = _df()
    out = MonitorRuleEngine()._apply_scope(df, {"id": "r_all", "scope": "all"})
    assert out.height == df.height
    assert sorted(out["symbol"].to_list()) == sorted(df["symbol"].to_list())


def test_apply_scope_symbols_filters_to_selection():
    df = _df()
    rule = {"id": "r_sym", "scope": "symbols", "symbols": ["600519.SH", "000001.SZ"]}
    out = MonitorRuleEngine()._apply_scope(df, rule)
    assert sorted(out["symbol"].to_list()) == ["000001.SZ", "600519.SH"]


def test_apply_scope_symbols_empty_returns_empty():
    rule = {"id": "r_sym", "scope": "symbols", "symbols": []}
    out = MonitorRuleEngine()._apply_scope(_df(), rule)
    assert out.is_empty()


# ── engine 端到端: sector 规则即使条件命中也不触发 (不当全市场) ──
def test_engine_sector_rule_never_fires_as_full_market():
    engine = MonitorRuleEngine()
    rule = {
        "id": "r_sector",
        "name": "板块规则",
        "type": "signal",
        "scope": "sector",
        "conditions": [{"field": "signal_golden", "op": "truth"}],
        "logic": "and",
        "cooldown_seconds": 0,
    }
    engine.set_rules([rule])
    df = pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "name": ["贵州茅台"],
            "close": [1700.0],
            "change_pct": [3.5],
            "signal_golden": [True],  # 条件本会命中
        }
    )
    # sector 规则 fail-closed → 0 事件, 绝不当作全市场对 600519 触发
    assert engine.evaluate(df) == []


def test_engine_symbols_rule_still_fires():
    # 对照组: symbols 规则保持原行为, 正常触发
    engine = MonitorRuleEngine()
    rule = {
        "id": "r_sym",
        "name": "指定股票",
        "type": "signal",
        "scope": "symbols",
        "symbols": ["600519.SH"],
        "conditions": [{"field": "signal_golden", "op": "truth"}],
        "logic": "and",
        "cooldown_seconds": 0,
    }
    engine.set_rules([rule])
    df = pl.DataFrame(
        {
            "symbol": ["600519.SH", "000001.SZ"],
            "name": ["贵州茅台", "平安银行"],
            "close": [1700.0, 12.0],
            "change_pct": [3.5, -1.0],
            "signal_golden": [True, True],
        }
    )
    events = engine.evaluate(df)
    assert len(events) == 1
    assert events[0]["symbol"] == "600519.SH"


# ── API options: 不再向新规则提供 sector ────────────────
def test_options_does_not_offer_sector(tmp_path: Path):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(monitor_rules_api.router)
    client = TestClient(app)

    resp = client.get("/api/monitor-rules/options")
    assert resp.status_code == 200
    scopes = [s["key"] for s in resp.json()["scopes"]]
    assert "sector" not in scopes
    # watchlist_group 为新增可选 scope (分组动态作用域)
    assert set(scopes) == {"symbols", "all", "watchlist_group"}
