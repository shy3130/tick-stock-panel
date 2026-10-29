"""数据状态接口 busy 降级 — 同步任务期间 /status 不再重算聚合。

回归 (2026-10-28 用户反馈): 同步任务运行时前端把 /api/data/status 轮询加速到
2s, 而聚合里的 DuckDB 查询与管道共用 repository 单连接全局锁 — 管道持锁跑长
查询时请求在锁上排队堆积, 反复触发前端 30s 超时 (请求超时（30s）/api/data/status)。
修复: busy (活跃任务/指标预热中) 时聚合退化为纯缓存读 (最后一次非空值), 不进锁。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import data as data_api
from app.services.pipeline_jobs import job_store


@pytest.fixture()
def clean_caches():
    data_api.invalidate_data_cache(None)
    data_api._table_cache_stale.clear()
    data_api._storage_cache_stale = None
    yield
    data_api.invalidate_data_cache(None)
    data_api._table_cache_stale.clear()
    data_api._storage_cache_stale = None


# ---------- _get_table_stats: busy 语义 ----------

def test_busy_serves_stale_without_fetch(clean_caches):
    calls = []

    def fetch():
        calls.append(1)
        return {"rows": 42}

    first = data_api._get_table_stats("daily", fetch)
    assert first == {"rows": 42} and len(calls) == 1
    # 失效事件 (stage 写完) 清 TTL 缓存, 但 stale 兜底保留
    data_api.invalidate_data_cache("daily")
    got = data_api._get_table_stats("daily", fetch, busy=True)
    assert got == {"rows": 42}
    assert len(calls) == 1  # busy 期间绝不重算 → 不进 DuckDB 锁


def test_busy_without_stale_returns_none_and_skips_fetch(clean_caches):
    calls = []

    def fetch():
        calls.append(1)
        return {"rows": 1}

    assert data_api._get_table_stats("minute", fetch, busy=True) is None
    assert calls == []


def test_not_busy_still_refetches_after_invalidation(clean_caches):
    calls = []

    def fetch():
        calls.append(1)
        return {"rows": len(calls)}

    data_api._get_table_stats("daily", fetch)
    data_api.invalidate_data_cache("daily")
    got = data_api._get_table_stats("daily", fetch)
    assert got == {"rows": 2} and len(calls) == 2  # 原有 TTL+事件语义不变


def test_get_storage_busy_serves_stale(clean_caches, tmp_path):
    data_api._get_storage(tmp_path)          # 首次计算并缓存
    data_api.invalidate_data_cache(None)     # 全清 (storage 一并清空)
    got = data_api._get_storage(tmp_path, busy=True)
    assert got is not None                   # stale 兜底仍在


def test_get_storage_busy_cold_start_returns_none(clean_caches, tmp_path):
    """服务刚启动、storage 缓存从未热过时, busy 期间返回 None (契约)。

    /api/data/status 的消费方 (如数据页存储卡片) 必须按 null 降级 —
    直接 s.storage.total_size_mb 会在预热/同步期间抛 TypeError
    (v0.3.22 修复 "Cannot read properties of null (reading 'total_size_mb')" 的依据)。
    """
    assert data_api._get_storage(tmp_path, busy=True) is None


# ---------- busy 判定 ----------

def _req(indicators_ready=True):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(indicators_ready=indicators_ready))
    )


def test_busy_when_indicators_warming(clean_caches):
    assert data_api._data_pipeline_busy(_req(indicators_ready=False)) is True


def test_busy_follows_active_job(monkeypatch, clean_caches):
    monkeypatch.setattr(job_store, "active_id", lambda: None)
    assert data_api._data_pipeline_busy(_req()) is False
    monkeypatch.setattr(job_store, "active_id", lambda: "abc123")
    monkeypatch.setattr(job_store, "get", lambda jid: {"status": "running"})
    assert data_api._data_pipeline_busy(_req()) is True
    # 终态任务不算 busy (防僵尸 active_id 把 /status 永久钉死在缓存)
    monkeypatch.setattr(job_store, "get", lambda jid: {"status": "succeeded"})
    assert data_api._data_pipeline_busy(_req()) is False


# ---------- status 端点: busy 接线 ----------

def test_status_endpoint_busy_skips_aggregates(monkeypatch, clean_caches, tmp_path):
    """indicators_ready=False → busy: 聚合全部跳过 (不触碰 repo), 轻字段照常返回。"""
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=repo, scheduler=None, indicators_ready=False,
    )))
    # 任何聚合尝试都算失败 — busy 下不应被调用
    def _boom(*_a, **_k):
        raise AssertionError("busy 下不应重算聚合")
    for fn in ("daily", "enriched", "index_daily", "minute", "financials"):
        monkeypatch.setattr(data_api, f"_safe_aggregate_{fn}", _boom)
    res = data_api.status(req)
    assert res["indicators_ready"] is False
    assert res["stats_pending"] is True
    assert res["checked_at"]
    assert res["daily"] is None and res["minute"] is None


@pytest.mark.parametrize("job_status", ["pending", "running"])
def test_status_marks_cold_stats_pending_until_job_finishes(
    monkeypatch, clean_caches, tmp_path, job_status,
):
    """预热已完成但同步/修复仍在运行时, null 统计不代表本地无数据。"""
    for table in ("kline_daily", "kline_daily_enriched"):
        (tmp_path / table / "date=2026-10-28").mkdir(parents=True)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=repo, scheduler=None, indicators_ready=True,
    )))
    monkeypatch.setattr(job_store, "active_id", lambda: "repair-job")
    monkeypatch.setattr(job_store, "get", lambda jid: {"status": job_status})
    calls = []

    def aggregate():
        calls.append(1)
        return {"trading_days": 240}

    monkeypatch.setattr(data_api, "_safe_aggregate_daily", lambda repo: aggregate())
    monkeypatch.setattr(data_api, "_safe_aggregate_enriched", lambda repo: aggregate())
    busy = data_api.status(req)
    assert busy["indicators_ready"] is True
    assert busy["stats_pending"] is True
    assert busy["daily"] is None and busy["enriched"] is None
    assert calls == []

    monkeypatch.setattr(job_store, "get", lambda jid: {"status": "succeeded"})
    ready = data_api.status(req)
    assert ready["stats_pending"] is False
    assert ready["daily"]["trading_days"] == 240
    assert ready["enriched"]["trading_days"] == 240
    assert len(calls) == 2
