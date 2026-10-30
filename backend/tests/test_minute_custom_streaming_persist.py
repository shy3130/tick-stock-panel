"""自定义分钟源流式落盘回归测试。

实测背景 (2026-09-30): 全市场分钟同步走自定义源时整表攒齐后一次落盘,
后端中途重启 (热重载) 丢掉 4h 进度、0 行落盘。修复: sync_minute_batch 的
持久化路径按 _CUSTOM_MINUTE_CHUNK 块拉取、每块立即 on_segment 落盘。
"""

from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from app.services import kline_sync

_MINUTE_DF_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}


def _minute_df(symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": symbols,
            "datetime": [datetime(2026, 9, 30, 10, 0)] * len(symbols),
            "open": [1.0] * len(symbols),
            "high": [1.0] * len(symbols),
            "low": [1.0] * len(symbols),
            "close": [1.0] * len(symbols),
            "volume": [100.0] * len(symbols),
            "amount": [None] * len(symbols),
        },
        schema=_MINUTE_DF_SCHEMA,
    )


def test_persist_path_pulls_and_persists_per_chunk(monkeypatch):
    """120 只 → 3 块 (50/50/20): 每块单独调用自定义源并立即 on_segment;
    进度按块计数 cur==total; 流式路径返回空 df (原契约)。"""
    calls: list[list[str]] = []
    segs: list[pl.DataFrame] = []
    progress: list[tuple[int, int, str]] = []

    def fake_try(symbols, **kwargs):
        calls.append(list(symbols))
        assert kwargs.get("on_chunk_done") is None, "块内不应再转发进度回调"
        return _minute_df(symbols), False

    monkeypatch.setattr(kline_sync, "_try_custom_minute", fake_try)
    monkeypatch.setattr(kline_sync, "_CUSTOM_MINUTE_CHUNK", 50)

    out = kline_sync.sync_minute_batch(
        [f"{i:06d}.SZ" for i in range(120)],
        on_chunk_done=lambda c, t, label=None: progress.append((c, t, label or "")),
        on_segment=segs.append,
    )

    assert [len(c) for c in calls] == [50, 50, 20]
    assert len(segs) == 3
    assert sum(len(s) for s in segs) == 120
    assert progress == [(1, 3, "custom"), (2, 3, "custom"), (3, 3, "custom")]
    assert out.is_empty()


def test_persist_path_empty_symbols_no_calls(monkeypatch):
    """空标的池: 不调用 provider, 直接返回空 df。"""
    calls: list[list[str]] = []

    monkeypatch.setattr(
        kline_sync,
        "_try_custom_minute",
        lambda symbols, **kw: calls.append(list(symbols)) or (_minute_df(symbols), False),
    )
    out = kline_sync.sync_minute_batch([], on_segment=lambda df: None)
    assert calls == []
    assert out.is_empty()


def test_persist_path_falls_through_to_tickflow_on_provider_failure(monkeypatch):
    """任一块 fallback → 整体落到 TickFlow 路径 (原语义), 不再调用剩余块。
    get_client 打桩避免真实网络; TickFlow 路径以"已调用"为断言即可。"""
    calls: list[list[str]] = []
    segs: list[pl.DataFrame] = []

    def fake_try(symbols, **kwargs):
        calls.append(list(symbols))
        return pl.DataFrame(schema=_MINUTE_DF_SCHEMA), True  # fallback

    monkeypatch.setattr(kline_sync, "_try_custom_minute", fake_try)
    monkeypatch.setattr(kline_sync, "_CUSTOM_MINUTE_CHUNK", 50)

    class _FakeTF:
        class klines:  # noqa: N801 - 与 tickflow SDK 形状一致即可
            @staticmethod
            def batch(*args, **kwargs):
                raise AssertionError("TickFlow 路径不应被本测试真实触达")

    monkeypatch.setattr(kline_sync, "get_client", lambda: _FakeTF())
    # TickFlow 分段路径会构造时间段并逐块调用; 这里只验证"落到了 TickFlow
    # 路径"(自定义源不再被继续调用), SDK 层抛错被 sync_minute_batch 捕获计数。
    kline_sync.sync_minute_batch(
        [f"{i:06d}.SZ" for i in range(60)],
        on_segment=segs.append,
    )
    assert len(calls) == 1  # 首块即 fallback, 不空打剩余块
    assert segs == []


def test_no_segment_path_returns_df_directly(monkeypatch):
    """未传 on_segment (实时补拉路径): 保持原契约, 整体一次调用并返回 df。"""
    calls: list[list[str]] = []

    def fake_try(symbols, **kwargs):
        calls.append(list(symbols))
        assert kwargs.get("on_chunk_done") is not None, "补拉路径保留进度回调"
        return _minute_df(symbols), False

    monkeypatch.setattr(kline_sync, "_try_custom_minute", fake_try)

    out = kline_sync.sync_minute_batch(
        ["600519.SH", "000001.SZ"],
        on_chunk_done=lambda c, t, label=None: None,
    )
    assert len(calls) == 1 and calls[0] == ["600519.SH", "000001.SZ"]
    assert out.height == 2


@pytest.mark.parametrize("streaming", [False, True])
def test_explicit_provider_failure_is_not_reported_as_successful_empty_sync(monkeypatch, streaming):
    from types import SimpleNamespace

    from app.data_providers.base import ProviderRequestError

    calls = []
    def fail(*args, **kwargs):
        calls.append(args[0])
        raise ProviderRequestError("数据源凭据或额度不可用")

    monkeypatch.setattr(kline_sync, "_selected_minute_provider_name", lambda: "example")
    monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda name: (SimpleNamespace(get_minute=fail), False, None))
    monkeypatch.setattr(kline_sync, "get_client", lambda: pytest.fail("不得跨源请求 TickFlow"))
    persisted = []
    with pytest.raises(ProviderRequestError, match="凭据或额度"):
        kline_sync.sync_minute_batch(["600519.SH", "000001.SZ"], on_segment=persisted.append if streaming else None)
    assert len(calls) == 1
    assert persisted == []


@pytest.mark.parametrize("days", [20, 365])
def test_background_minute_sync_marks_provider_denial_as_failed(monkeypatch, tmp_path, days):
    import asyncio
    from types import SimpleNamespace

    from app.api import kline as kline_api
    from app.data_providers.base import ProviderRequestError
    from app.services import pipeline_jobs

    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "try_acquire_run_slot", lambda job_id: True)
    monkeypatch.setattr(pipeline_jobs, "release_run_slot", lambda job_id: None)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda job_id, fn: fn())
    monkeypatch.setattr(kline_api, "_minute_allowed", lambda capset: True)
    monkeypatch.setattr("app.tickflow.pools.get_pool", lambda name: ["600519.SH"])
    monkeypatch.setattr("app.api.data.invalidate_storage_cache", lambda: None)
    def denied(*args, **kwargs):
        raise ProviderRequestError("TdxQuant 凭据、接口权限或额度不可用")
    monkeypatch.setattr(kline_sync, "sync_and_persist_minute", denied)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_index_symbol_set=lambda: set())
    async def run():
        tasks = []
        create_task = asyncio.create_task
        def capture(coro):
            task = create_task(coro)
            tasks.append(task)
            return task
        monkeypatch.setattr(asyncio, "create_task", capture)
        result = await kline_api.trigger_minute_sync(repo, object(), override_days=days)
        await tasks[0]
        return store.get(result["job_id"])
    result = asyncio.run(run())
    assert result["status"] == "failed"
    assert "凭据、接口权限或额度" in result["error"]
    assert not list(tmp_path.rglob("*.parquet"))


def test_provider_request_error_has_readable_http_response():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.data_providers.base import ProviderRequestError
    from app.main import provider_request_error_handler

    app = FastAPI()
    app.add_exception_handler(ProviderRequestError, provider_request_error_handler)
    @app.get("/minute")
    def minute():
        raise ProviderRequestError("数据源凭据或额度不可用")
    response = TestClient(app).get("/minute")
    assert response.status_code == 502
    assert response.json() == {"detail": "数据源凭据或额度不可用", "code": "provider_request"}
