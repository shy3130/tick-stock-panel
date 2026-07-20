"""回归测试: 本轮修复的几处高风险行为(并发单飞 / 重任务槽 / sector fail-closed)。

均为纯逻辑, 不触网, 不依赖真实数据源。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.services import pipeline_jobs, quote_service
from app.services.pipeline_jobs import JobStore
from app.services.quote_service import QuoteService
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine

# ── JobStore 单飞 ────────────────────────────────────────────────────────

def test_create_singleflight_dedupes_pending_window(tmp_path):
    """两次快速 create() 在 pending 窗口内应复用同一 job(is_new=False)。"""
    store = JobStore(store_dir=tmp_path / "jobs")

    jid1, new1 = store.create()
    assert new1 is True

    # 尚未 start(), job 仍是 pending —— 旧实现会在此另起新 job(并发双跑根因)
    jid2, new2 = store.create()
    assert jid2 == jid1
    assert new2 is False

    # start() 后仍复用同一活跃 job
    store.start(jid1)
    jid3, new3 = store.create()
    assert jid3 == jid1
    assert new3 is False


def test_create_new_after_terminal(tmp_path):
    """job 终态(succeed/fail)后, create() 应给出新 job。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid1, _ = store.create()
    store.start(jid1)
    store.succeed(jid1, {"ok": True})

    jid2, new2 = store.create()
    assert jid2 != jid1
    assert new2 is True


def test_run_slot_is_exclusive():
    """重任务执行槽同一时刻只允许一个持有者(防僵尸并发)。"""
    assert pipeline_jobs.try_acquire_run_slot() is True
    try:
        # 已被占用, 第二次获取失败
        assert pipeline_jobs.try_acquire_run_slot() is False
    finally:
        pipeline_jobs.release_run_slot()
    # 释放后可再次获取
    assert pipeline_jobs.try_acquire_run_slot() is True
    pipeline_jobs.release_run_slot()
    # 重复释放幂等, 不抛
    pipeline_jobs.release_run_slot()


# ── 监控 sector fail-closed ──────────────────────────────────────────────

def _base_price_rule(scope: str) -> dict:
    return {
        "id": "r_test",
        "name": "t",
        "type": "price",
        "conditions": [{"field": "close", "op": ">", "value": 10}],
        "logic": "and",
        "scope": scope,
    }


def test_validate_rejects_sector_scope():
    with pytest.raises(ValueError):
        monitor_rules.validate(_base_price_rule("sector"))


def test_validate_accepts_symbols_scope():
    rule = _base_price_rule("symbols")
    rule["symbols"] = ["600000.SH"]
    monitor_rules.validate(rule)  # 不应抛


def test_apply_scope_sector_fails_closed():
    """历史遗留 sector 规则在评估时应返回空(绝不退化为全市场)。"""
    df = pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "close": [10.0, 20.0]})
    out = MonitorRuleEngine._apply_scope(df, {"id": "r_old", "scope": "sector"})
    assert out.is_empty()

    # 对照: scope=all 返回全量, symbols 过滤子集
    assert MonitorRuleEngine._apply_scope(df, {"scope": "all"}).height == 2
    picked = MonitorRuleEngine._apply_scope(
        df, {"scope": "symbols", "symbols": ["600000.SH"]}
    )
    assert picked.height == 1


def test_ladder_webhook_uses_chinese_title_without_brand(monkeypatch):
    calls = []

    class CaptureExecutor:
        def submit(self, fn, *args):
            calls.append((fn, args))

    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", CaptureExecutor())
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: "https://open.feishu.cn/open-apis/bot/v2/hook/test")
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_secret", lambda: "secret")
    monkeypatch.setattr("app.services.preferences.get_wecom_webhook_url", lambda: "wecom-key")

    engine = type("Engine", (), {
        "rules": {"r_ladder": {"webhook_channels": ["feishu", "wecom"]}},
    })()
    QuoteService._maybe_send_webhook(
        object.__new__(QuoteService),
        [{
            "rule_id": "r_ladder",
            "source": "ladder",
            "symbol": "600000.SH",
            "name": "浦发银行",
            "message": "炸板预警",
        }],
        engine,
    )

    assert [args[1] for _, args in calls] == ["连板梯队", "连板梯队"]
    assert all("TickFlow" not in args[1] for _, args in calls)


def test_review_webhooks_use_title_without_brand(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.preferences.get_review_push_channels", lambda: ["feishu", "wecom"])
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: "feishu-url")
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_secret", lambda: "secret")
    monkeypatch.setattr("app.services.preferences.get_wecom_webhook_url", lambda: "wecom-url")
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_feishu_card",
        lambda *args: calls.append(("feishu", args)) or True,
    )
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_wecom_markdown",
        lambda *args: calls.append(("wecom", args)) or True,
    )

    daily_pipeline._maybe_push_review("复盘正文", {"as_of": "2026-07-18"})

    assert [args[1] for _, args in calls] == ["每日复盘", "每日复盘"]
    assert all("TickFlow" not in args[1] for _, args in calls)
