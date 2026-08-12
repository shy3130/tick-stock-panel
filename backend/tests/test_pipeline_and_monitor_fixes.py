"""回归测试: 本轮修复的几处高风险行为(并发单飞 / 重任务槽 / sector fail-closed)。

均为纯逻辑, 不触网, 不依赖真实数据源。
"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest

from app.api import pipeline as pipeline_api
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


def test_reap_stale_keeps_run_slot_until_executor_really_finishes(tmp_path):
    """job 超时只改变可见状态, 不能放行仍在写盘的僵尸执行体。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    assert pipeline_jobs.try_acquire_run_slot() is True
    try:
        job_id, _ = store.create(timeout_s=0)
        store.start(job_id)
        store._active_jobs[job_id]["started_at"] = "2000-01-01T00:00:00Z"

        store.reap_stale(timeout_s=0)

        assert store.get(job_id)["status"] == "failed"
        assert pipeline_jobs.try_acquire_run_slot() is False
    finally:
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


# ── 盘后数据与策略缓存同日闭环 ──────────────────────────────────────

def test_scheduled_pipeline_refreshes_strategy_cache_after_daily_data(
    monkeypatch,
    tmp_path,
):
    """定时盘后任务成功后必须重算策略,避免行情和策略日期错一天。"""
    events: list[str] = []
    jobs: dict[str, object] = {}

    class FakeScheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, fn, *args, id: str, **kwargs):
            jobs[id] = fn

        def start(self):
            pass

    class FakeRepo:
        store = SimpleNamespace(data_dir=tmp_path)

        def refresh_cache(self):
            events.append("repo_refresh")

    engine = object()
    app_state = SimpleNamespace(
        capabilities=object(),
        quote_service=None,
        strategy_engine=engine,
    )

    monkeypatch.setattr(daily_pipeline, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: app_state)
    monkeypatch.setattr(
        daily_pipeline,
        "_run_tracked",
        lambda fn, _label: fn(on_progress=None),
    )
    monkeypatch.setattr(
        daily_pipeline,
        "run_now",
        lambda *_args, **_kwargs: events.append("daily_data") or {"ok": True},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "refresh_strategy_cache",
        lambda repo, actual_engine: (
            events.append("strategy_cache")
            or {"as_of": "2026-07-29", "strategy_count": 3}
        ),
        raising=False,
    )
    monkeypatch.setattr(
        daily_pipeline,
        "publish_research_snapshot",
        lambda _data_dir: (
            events.append("research_snapshot")
            or {"snapshot_id": "snapshot-1", "as_of": "2026-07-29"}
        ),
    )
    monkeypatch.setattr(
        "app.services.preferences.get_pipeline_schedule",
        lambda: {"hour": 15, "minute": 30},
    )
    monkeypatch.setattr(
        "app.services.preferences.get_instruments_schedule",
        lambda: {"hour": 9, "minute": 10},
    )
    monkeypatch.setattr(
        "app.services.preferences.get_depth_finalize_time",
        lambda: {"hour": 15, "minute": 2},
    )
    monkeypatch.setattr(
        "app.services.preferences.get_review_schedule",
        lambda: {"enabled": False, "hour": 15, "minute": 10},
    )

    daily_pipeline.start_scheduler(FakeRepo(), object())
    jobs["daily_pipeline"]()

    assert events == [
        "daily_data",
        "repo_refresh",
        "strategy_cache",
        "research_snapshot",
    ]


def test_post_market_data_failure_refreshes_repo_but_skips_strategies(monkeypatch):
    """行情管道失败时保留已落盘数据,但绝不能用不完整数据重算策略。"""
    events: list[str] = []

    class FakeRepo:
        def refresh_cache(self):
            events.append("repo_refresh")

    def fail_daily(*_args, **_kwargs):
        events.append("daily_data")
        raise RuntimeError("data failed")

    monkeypatch.setattr(daily_pipeline, "run_now", fail_daily)
    monkeypatch.setattr(
        daily_pipeline,
        "refresh_strategy_cache",
        lambda *_args, **_kwargs: events.append("strategy_cache"),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="data failed"):
        daily_pipeline.run_post_market(
            FakeRepo(),
            object(),
            app_state=SimpleNamespace(
                capabilities=object(),
                quote_service=None,
                strategy_engine=object(),
            ),
        )

    assert events == ["daily_data", "repo_refresh"]


def test_post_market_publishes_research_snapshot_after_strategy_cache(monkeypatch):
    """The visible research bundle is the final commit point of a successful run."""
    events: list[str] = []

    class FakeRepo:
        store = SimpleNamespace(data_dir="data-dir")

        def refresh_cache(self):
            events.append("repo_refresh")

    monkeypatch.setattr(
        daily_pipeline,
        "run_now",
        lambda *_args, **_kwargs: events.append("daily_data") or {"ok": True},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "refresh_strategy_cache",
        lambda *_args, **_kwargs: (
            events.append("strategy_cache")
            or {"as_of": "2026-07-31", "strategy_count": 3, "results": {}}
        ),
    )
    monkeypatch.setattr(
        daily_pipeline,
        "publish_research_snapshot",
        lambda data_dir: (
            events.append(("research_snapshot", data_dir))
            or {"snapshot_id": "snapshot-1", "as_of": "2026-07-31"}
        ),
        raising=False,
    )

    result = daily_pipeline.run_post_market(
        FakeRepo(),
        object(),
        app_state=SimpleNamespace(
            capabilities=object(),
            quote_service=None,
            strategy_engine=object(),
        ),
    )

    assert events == [
        "daily_data",
        "repo_refresh",
        "strategy_cache",
        ("research_snapshot", "data-dir"),
    ]
    assert result["research_snapshot"] == {
        "snapshot_id": "snapshot-1",
        "as_of": "2026-07-31",
    }


@pytest.mark.asyncio
async def test_manual_pipeline_uses_same_post_market_executor(monkeypatch):
    """手动刷新与定时刷新必须共用入口,不能再维护两套收尾逻辑。"""
    events: list[str] = []
    scheduled = []

    class FakeJobStore:
        def reap_stale(self):
            pass

        def create(self):
            return "job-1", True

        def start(self, _job_id):
            events.append("job_start")

        def progress(self, *_args, **_kwargs):
            pass

        def succeed(self, _job_id, result):
            events.append(("job_success", result))

        def fail(self, _job_id, message):
            events.append(("job_failed", message))

    class FakeRepo:
        store = SimpleNamespace(data_dir=None)

        def refresh_cache(self):
            events.append("duplicate_repo_refresh")

    def capture_task(coro):
        scheduled.append(coro)
        return SimpleNamespace()

    monkeypatch.setattr(pipeline_api, "job_store", FakeJobStore())
    monkeypatch.setattr(pipeline_api, "try_acquire_run_slot", lambda: True)
    monkeypatch.setattr(pipeline_api, "release_run_slot", lambda: None)
    monkeypatch.setattr(pipeline_api, "invalidate_storage_cache", lambda: None)
    monkeypatch.setattr(pipeline_api.asyncio, "create_task", capture_task)
    monkeypatch.setattr(
        daily_pipeline,
        "run_now",
        lambda *_args, **_kwargs: events.append("legacy_pipeline") or {"legacy": True},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "run_post_market",
        lambda *_args, **_kwargs: (
            events.append("post_market")
            or {"strategy_cache": {"as_of": "2026-07-29"}}
        ),
    )

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=FakeRepo(),
                capabilities=object(),
                quote_service=None,
                strategy_engine=object(),
            )
        )
    )
    response = await pipeline_api.run_now(request)
    await scheduled[0]

    assert response == {"job_id": "job-1", "reused": False}
    assert events == [
        "job_start",
        "post_market",
        ("job_success", {"strategy_cache": {"as_of": "2026-07-29"}}),
    ]
