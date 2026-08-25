"""监控规则 webhook 推送测试 — 命中路径按需 POST,失败不阻塞告警。

覆盖两类入口:
1. _push_rule_webhook (纯函数级): 启用/未启用/异常不抛。
2. MonitorRuleEngine._evaluate_rule (集成): 规则命中时触发推送且不影响返回的 events。
"""

from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest

from app.strategy import monitor
from app.strategy.monitor import MonitorRuleEngine, _push_rule_webhook


# ── 直接测试 _push_rule_webhook ────────────────────────────
def _ev(**overrides):
    base = {
        "ts": 1700000000000,
        "rule_id": "r1",
        "rule_name": "规则一",
        "symbol": "600519.SH",
        "severity": "warning",
        "message": "触发",
    }
    base.update(overrides)
    return base


def test_push_webhook_when_enabled(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("httpx.post", fake_post)

    rule = {"id": "r1", "webhook_enabled": True, "webhook_url": "https://hook.test/r1"}
    _push_rule_webhook(rule, _ev())

    assert len(calls) == 1
    assert calls[0][0] == "https://hook.test/r1"
    body = calls[0][1]["json"]
    assert body["rule_id"] == "r1"
    assert body["symbol"] == "600519.SH"
    assert body["message"] == "触发"
    assert calls[0][1]["timeout"] == 3.0
    assert calls[0][1]["trust_env"] is False


def test_push_webhook_skipped_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr("httpx.post", lambda url, **kw: calls.append((url, kw)))
    rule = {"id": "r1", "webhook_enabled": False, "webhook_url": "https://hook.test/r1"}
    _push_rule_webhook(rule, _ev())
    assert calls == []


def test_push_webhook_skipped_when_no_url(monkeypatch):
    calls = []
    monkeypatch.setattr("httpx.post", lambda url, **kw: calls.append((url, kw)))
    rule = {"id": "r1", "webhook_enabled": True, "webhook_url": ""}
    _push_rule_webhook(rule, _ev())
    assert calls == []


def test_push_webhook_skipped_when_disabled_by_default(monkeypatch):
    # webhook_enabled 未设默认不推送 (normalize 时默认 False)
    calls = []
    monkeypatch.setattr("httpx.post", lambda url, **kw: calls.append((url, kw)))
    rule = {"id": "r1", "webhook_url": "https://hook.test/r1"}
    _push_rule_webhook(rule, _ev())
    assert calls == []


def test_push_webhook_exception_does_not_raise(monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("httpx.post", boom)
    rule = {"id": "r1", "webhook_enabled": True, "webhook_url": "https://hook.test/r1"}
    # 不抛异常即通过 (失败被吞成 warning)
    _push_rule_webhook(rule, _ev())


# ── 集成: 规则命中时触发推送且不影响 events 返回 ──────────
def _hit_df():
    return pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "name": ["贵州茅台"],
            "close": [1700.0],
            "change_pct": [3.5],
            "signal_golden": [True],
        }
    )


def _signal_rule():
    return {
        "id": "r1",
        "name": "金叉",
        "type": "signal",
        "scope": "symbols",
        "symbols": ["600519.SH"],
        "conditions": [{"field": "signal_golden", "op": "truth"}],
        "logic": "and",
        "cooldown_seconds": 0,
        "severity": "warning",
        "message": "金叉触发",
    }


def test_engine_rule_hit_triggers_webhook(monkeypatch):
    calls = []
    monkeypatch.setattr("httpx.post", lambda url, **kw: calls.append((url, kw)))

    engine = MonitorRuleEngine()
    rule = _signal_rule()
    rule["webhook_enabled"] = True
    rule["webhook_url"] = "https://hook.test/r1"
    engine.set_rules([rule])

    events = engine.evaluate(_hit_df())
    assert len(events) == 1
    assert events[0]["symbol"] == "600519.SH"
    assert len(calls) == 1
    assert calls[0][1]["json"]["rule_id"] == "r1"


def test_engine_rule_hit_without_webhook_does_not_post(monkeypatch):
    calls = []
    monkeypatch.setattr("httpx.post", lambda url, **kw: calls.append((url, kw)))

    engine = MonitorRuleEngine()
    rule = _signal_rule()  # webhook 未启用
    engine.set_rules([rule])

    events = engine.evaluate(_hit_df())
    assert len(events) == 1
    assert calls == []


def test_engine_webhook_failure_does_not_block_alert(monkeypatch):
    def boom(url, **kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr("httpx.post", boom)

    engine = MonitorRuleEngine()
    rule = _signal_rule()
    rule["webhook_enabled"] = True
    rule["webhook_url"] = "https://hook.test/r1"
    engine.set_rules([rule])

    # 推送异常不应让 evaluate 失败,事件照常返回
    events = engine.evaluate(_hit_df())
    assert len(events) == 1
    assert events[0]["message"] == "金叉触发"
