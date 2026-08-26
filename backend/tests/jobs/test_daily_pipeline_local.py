from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import polars as pl

from app.jobs import daily_pipeline
from app.jobs.daily_pipeline import _latest_enriched_date
from app.services import preferences
from app.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.data_providers.fquant.catalog_resolver import CatalogError


def test_latest_enriched_date_uses_partition_names(tmp_path):
    for ds in ("2026-07-01", "bad", "2026-07-02"):
        (tmp_path / "kline_daily_enriched" / f"date={ds}").mkdir(parents=True)

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    assert _latest_enriched_date(repo) == date(2026, 7, 2)


def test_provider_freshness_uses_full_daily_fallback_chain(monkeypatch):
    provider = SimpleNamespace(
        get_daily_freshness=lambda: date(2026, 8, 17),
        _engine=SimpleNamespace(
            freshness=lambda: (_ for _ in ()).throw(
                AssertionError("must prefer provider freshness")
            )
        ),
    )
    monkeypatch.setattr("app.data_providers.get_provider", lambda _name: provider)
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda _capability=None: "fquant_local",
    )

    assert daily_pipeline._provider_freshness_date() == date(2026, 8, 17)


def test_run_now_local_mode_skips_raw_sync_and_runs_local_pipeline(tmp_path, monkeypatch):
    calls = {"local": 0, "raw": 0}
    published: list[date] = []
    refreshed: list[bool] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: None,
        refresh_cache=lambda: refreshed.append(True),
    )

    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda data_dir: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset: ["600519.SH"])
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda repo: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda repo, name: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda repo: None)
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_index", lambda: False)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_etf", lambda: False)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_hk", lambda: False)
    monkeypatch.setattr(preferences, "get_minute_sync_enabled", lambda: False)
    monkeypatch.setattr(preferences, "get_minute_sync_days", lambda: 5)
    monkeypatch.setattr(
        daily_pipeline.kline_sync,
        "sync_and_persist_daily_batch",
        lambda *a, **k: calls.__setitem__("raw", calls["raw"] + 1),
    )
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: object())
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    fresh = date(2026, 7, 3)
    monkeypatch.setattr(
        daily_pipeline,
        "initialize_local_enriched_ceiling",
        lambda _repo: published.append(fresh) or fresh,
    )

    def fake_local(*args, **kwargs):
        calls["local"] += 1
        return 7

    monkeypatch.setattr(daily_pipeline, "run_pipeline_local", fake_local)

    result = daily_pipeline.run_now(repo, CapabilitySet())

    assert calls == {"local": 1, "raw": 0}
    assert result["enriched_rows"] == 7
    assert published == [fresh]
    assert refreshed == [True]


def test_run_now_marks_minute_catalog_failure_as_degraded(tmp_path, monkeypatch):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: None,
    )
    refreshed = []

    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda data_dir: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset: ["600519.SH"])
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda repo: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda repo, name: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda repo: refreshed.append(True))
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_index", lambda: False)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_etf", lambda: False)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_hk", lambda: False)
    monkeypatch.setattr(preferences, "get_minute_sync_enabled", lambda: True)
    monkeypatch.setattr(preferences, "get_minute_sync_days", lambda: 5)
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: object())
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    monkeypatch.setattr(daily_pipeline, "run_pipeline_local", lambda *args, **kwargs: 7)
    monkeypatch.setattr(
        daily_pipeline.kline_sync,
        "sync_and_persist_minute",
        lambda *args, **kwargs: (_ for _ in ()).throw(CatalogError("broken catalog")),
    )

    result = daily_pipeline.run_now(
        repo,
        CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=100)}),
    )

    assert result["minute_rows"] == 0
    assert result["failed_stages"] == [
        {"stage": "sync_minute", "error": "broken catalog"},
    ]
    assert refreshed == [True]


def test_initialize_local_enriched_ceiling_publishes_only_verified_partition(tmp_path, monkeypatch):
    """完整当日分区才发布 canonical —— 完整分区通过 coverage 校验后发布 fresh。"""
    published: list[date] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        set_enriched_canonical_date=published.append,
    )
    fresh = date(2026, 7, 3)
    # 前一天分区 + 当天分区，symbol 数量达标（coverage ≥ 90%）
    prev = tmp_path / "kline_daily_enriched" / "date=2026-07-02"
    curr = tmp_path / "kline_daily_enriched" / "date=2026-07-03"
    prev.mkdir(parents=True)
    curr.mkdir(parents=True)
    pl.DataFrame({"symbol": list("ABCDE")}).write_parquet(prev / "part.parquet")
    pl.DataFrame({"symbol": list("ABCDE")}).write_parquet(curr / "part.parquet")

    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline, "_provider_freshness_date", lambda: fresh)

    assert daily_pipeline.initialize_local_enriched_ceiling(repo) == fresh
    assert published == [fresh]


def test_initialize_local_enriched_ceiling_withholds_incomplete_partition(tmp_path, monkeypatch):
    """不完整当日分区不发布 canonical —— 退回到已验证的前一完整分区。"""
    published: list[date] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        set_enriched_canonical_date=published.append,
    )
    fresh = date(2026, 7, 3)
    prev = tmp_path / "kline_daily_enriched" / "date=2026-07-02"
    curr = tmp_path / "kline_daily_enriched" / "date=2026-07-03"
    prev.mkdir(parents=True)
    curr.mkdir(parents=True)
    # 前一天 5 只，当天只有 1 只 → coverage 20% < 90%，判定不完整
    pl.DataFrame({"symbol": list("ABCDE")}).write_parquet(prev / "part.parquet")
    pl.DataFrame({"symbol": ["A"]}).write_parquet(curr / "part.parquet")

    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline, "_provider_freshness_date", lambda: fresh)

    # 退回到前一完整分区 2026-07-02，而非发布不完整的 2026-07-03
    assert daily_pipeline.initialize_local_enriched_ceiling(repo) == date(2026, 7, 2)
    assert published == [date(2026, 7, 2)]


def test_initialize_local_enriched_ceiling_withholds_when_pull_disabled(tmp_path, monkeypatch):
    """pipeline_pull_a_share 关闭时也不越过校验发布未验证日期。

    fresh 分区不存在于磁盘 → 无已验证分区可发布，不泄露未验证水位。
    """
    published: list[date] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        set_enriched_canonical_date=published.append,
    )
    fresh = date(2026, 7, 3)
    # 磁盘上只有一个旧分区（不等于 fresh），无法通过 coverage 校验
    old = tmp_path / "kline_daily_enriched" / "date=2026-07-01"
    old.mkdir(parents=True)
    pl.DataFrame({"symbol": list("ABCDE")}).write_parquet(old / "part.parquet")

    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: False)
    monkeypatch.setattr(daily_pipeline, "_provider_freshness_date", lambda: fresh)

    # 旧分区通过 coverage（无 previous → target_count > 0）→ 发布安全旧日期，不泄露 fresh
    result = daily_pipeline.initialize_local_enriched_ceiling(repo)
    assert result is not None
    assert result < fresh
    assert fresh not in published


def test_bootstrap_local_enriched_skips_incomplete_new_date(tmp_path, monkeypatch):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        refresh_cache=lambda: (_ for _ in ()).throw(AssertionError("should not refresh")),
    )
    (tmp_path / "kline_daily_enriched" / "date=2026-07-02").mkdir(parents=True)
    calls = {"run": 0}

    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(daily_pipeline, "_provider_freshness_date", lambda: date(2026, 7, 3))
    monkeypatch.setattr(daily_pipeline, "_local_daily_coverage_ok", lambda target: False)
    monkeypatch.setattr(
        daily_pipeline, "run_now", lambda *a, **k: calls.__setitem__("run", calls["run"] + 1)
    )

    result = daily_pipeline.bootstrap_local_enriched_if_stale(repo, CapabilitySet())

    assert result["reason"] == "incomplete"
    assert calls["run"] == 0


def test_bootstrap_ignores_unconfirmed_future_partition_without_deleting_it(
    tmp_path,
    monkeypatch,
):
    published: list[date] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        set_enriched_canonical_date=published.append,
        refresh_cache=lambda: None,
    )
    for ds in ("2026-07-03", "2026-07-04"):
        (tmp_path / "kline_daily_enriched" / f"date={ds}").mkdir(parents=True)

    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(
        daily_pipeline,
        "_provider_freshness_date",
        lambda: date(2026, 7, 3),
    )
    monkeypatch.setattr(
        daily_pipeline,
        "_local_partition_coverage_ok",
        lambda repo, previous, target: True,
    )

    result = daily_pipeline.bootstrap_local_enriched_if_stale(
        repo,
        CapabilitySet(),
    )

    assert result == {
        "started": False,
        "reason": "up_to_date",
        "freshness": "2026-07-03",
        "enriched": "2026-07-03",
    }
    assert published == [date(2026, 7, 3)]
    assert (tmp_path / "kline_daily_enriched" / "date=2026-07-04").exists()


def test_bootstrap_local_enriched_runs_when_fresh_complete(tmp_path, monkeypatch):
    refreshed = {"ok": False}
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        refresh_cache=lambda: refreshed.__setitem__("ok", True),
    )
    (tmp_path / "kline_daily_enriched" / "date=2026-07-02").mkdir(parents=True)

    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(daily_pipeline, "_provider_freshness_date", lambda: date(2026, 7, 3))
    monkeypatch.setattr(daily_pipeline, "_local_daily_coverage_ok", lambda target: True)
    monkeypatch.setattr(
        daily_pipeline, "_local_partition_coverage_ok", lambda repo, previous, target: True
    )
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset: ["600519.SH"])
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: object())
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    monkeypatch.setattr(daily_pipeline, "run_pipeline_local_incremental", lambda *a, **k: 5)

    result = daily_pipeline.bootstrap_local_enriched_if_stale(repo, CapabilitySet())

    assert result["started"] is True
    assert refreshed["ok"] is True
    assert result["written"] == 5


def test_bootstrap_local_enriched_rejects_partial_partition(tmp_path, monkeypatch):
    refreshed = {"ok": False}
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        refresh_cache=lambda: refreshed.__setitem__("ok", True),
    )
    (tmp_path / "kline_daily_enriched" / "date=2026-07-02").mkdir(parents=True)

    monkeypatch.setattr(daily_pipeline, "is_local_daily_mode", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(daily_pipeline, "_provider_freshness_date", lambda: date(2026, 7, 3))
    monkeypatch.setattr(daily_pipeline, "_local_daily_coverage_ok", lambda target: True)
    monkeypatch.setattr(
        daily_pipeline, "_local_partition_coverage_ok", lambda repo, previous, target: False
    )
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset: ["600519.SH"])
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: object())
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    monkeypatch.setattr(daily_pipeline, "run_pipeline_local_incremental", lambda *a, **k: 1)

    result = daily_pipeline.bootstrap_local_enriched_if_stale(repo, CapabilitySet())

    assert result["reason"] == "partition_incomplete"
    assert result["written"] == 1
    assert refreshed["ok"] is True


def test_local_daily_coverage_gate_requires_most_sample_symbols(monkeypatch):
    class Provider:
        def get_daily(self, symbols, start, end, asset_type):
            return pl.DataFrame(
                {"symbol": ["A", "B"], "date": [date(2026, 7, 3), date(2026, 7, 3)]}
            )

    monkeypatch.setattr("app.data_providers.get_provider", lambda name: Provider())
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )

    assert daily_pipeline._local_daily_coverage_ok(date(2026, 7, 3), ("A", "B", "C")) is False
    assert daily_pipeline._local_daily_coverage_ok(date(2026, 7, 3), ("A", "B")) is True


def test_local_partition_coverage_compares_previous_day(tmp_path):
    previous = tmp_path / "kline_daily_enriched" / "date=2026-07-02"
    target = tmp_path / "kline_daily_enriched" / "date=2026-07-03"
    previous.mkdir(parents=True)
    target.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A", "B", "C", "D"]}).write_parquet(previous / "part.parquet")
    pl.DataFrame({"symbol": ["A", "B"]}).write_parquet(target / "part.parquet")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    assert (
        daily_pipeline._local_partition_coverage_ok(repo, date(2026, 7, 2), date(2026, 7, 3))
        is False
    )

    pl.DataFrame({"symbol": ["A", "B", "C", "D"]}).write_parquet(target / "part.parquet")
    assert (
        daily_pipeline._local_partition_coverage_ok(repo, date(2026, 7, 2), date(2026, 7, 3))
        is True
    )


def test_provider_refresh_fstore_clients_refreshes_both(monkeypatch):
    """FQuantProvider.refresh_fstore_clients 同时刷新 _fstore 和 _fstore_markets，并清空 instruments 缓存。"""
    from app.data_providers.fquant_provider import FQuantProvider

    calls: list[str] = []

    class SpyClient:
        def refresh(self):
            calls.append("refresh")

    provider = FQuantProvider.__new__(FQuantProvider)
    provider._fstore = SpyClient()
    provider._fstore_markets = SpyClient()
    provider._instruments_cache = {"stock": pl.DataFrame({"symbol": ["OLD"]})}
    provider._instruments_cache_ts = {"stock": datetime(2026, 1, 1)}
    provider._instruments_cache_ttl = 86400

    provider.refresh_fstore_clients()

    assert calls == ["refresh", "refresh"]
    # 24h TTL instruments 缓存必须被清空，否则 get_instruments 返回旧 generation 标的
    assert provider._instruments_cache == {}
    assert provider._instruments_cache_ts == {}


def test_provider_refresh_fstore_clients_dedup_same_instance(monkeypatch):
    """同一实例 _fstore == _fstore_markets 时只刷新一次，instruments 缓存仍清空。"""
    from app.data_providers.fquant_provider import FQuantProvider

    calls: list[str] = []

    class SpyClient:
        def refresh(self):
            calls.append("refresh")

    provider = FQuantProvider.__new__(FQuantProvider)
    shared = SpyClient()
    provider._fstore = shared
    provider._fstore_markets = shared
    provider._instruments_cache = {"index": pl.DataFrame({"symbol": ["IDX"]})}
    provider._instruments_cache_ts = {"index": datetime(2026, 1, 1)}
    provider._instruments_cache_ttl = 86400

    provider.refresh_fstore_clients()

    assert calls == ["refresh"]
    assert provider._instruments_cache == {}
    assert provider._instruments_cache_ts == {}


def test_invalidate_clears_overview_cache(monkeypatch):
    """_invalidate 同步失效 overview 聚合缓存。"""
    overview_cleared: list[bool] = []
    data_cleared: list = []

    monkeypatch.setattr(
        "app.api.data.invalidate_data_cache",
        lambda table=None: data_cleared.append(table),
    )
    monkeypatch.setattr(
        "app.api.overview.invalidate_overview_cache",
        lambda: overview_cleared.append(True),
    )

    daily_pipeline._invalidate("daily")

    assert data_cleared == ["daily"]
    assert overview_cleared == [True]


def test_run_instruments_sync_refreshes_repo_cache(tmp_path, monkeypatch):
    """盘前 instruments sync 后 repo 内存缓存立即刷新，getter 看到新内容。"""
    refreshed: list[bool] = []
    invalidated: list = []

    monkeypatch.setattr(
        daily_pipeline.instrument_sync,
        "sync_instruments",
        lambda data_dir: 42,
    )
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda repo: None)
    monkeypatch.setattr(
        "app.api.data.invalidate_data_cache",
        lambda table=None: invalidated.append(table),
    )
    monkeypatch.setattr(
        "app.api.overview.invalidate_overview_cache",
        lambda: None,
    )

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        refresh_instruments_cache=lambda: refreshed.append(True),
    )

    result = daily_pipeline.run_instruments_sync(repo)

    assert result == {"instruments_rows": 42}
    assert refreshed == [True]
    assert invalidated == ["instruments"]


def test_run_tracked_marks_kind_and_invalidates_terminal_status_cache(tmp_path, monkeypatch):
    from app.services import pipeline_jobs
    from app.services.pipeline_jobs import JobStore

    store = JobStore(store_dir=tmp_path)
    invalidated: list[bool] = []
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(
        "app.api.data.invalidate_job_status_cache",
        lambda: invalidated.append(True),
    )

    def fail_before_progress(*, on_progress):
        raise RuntimeError("instrument sync failed")

    daily_pipeline._run_tracked(fail_before_progress, "daily_pipeline")

    job = store.list_recent(limit=1)[0]
    assert job["status"] == "failed"
    assert job["kind"] == "daily_pipeline"
    assert job["stage"] == "init"
    assert invalidated == [True]


def test_run_tracked_reaps_stalled_job_without_releasing_live_worker_slot(tmp_path, monkeypatch):
    from app.services import pipeline_jobs
    from app.services.pipeline_jobs import JobStore, STALL_TIMEOUT_S

    store = JobStore(store_dir=tmp_path)
    stale_id, _ = store.create(kind="daily_pipeline")
    stale_owner = store.start(stale_id)
    stale_at = (datetime.now(timezone.utc) - timedelta(seconds=STALL_TIMEOUT_S + 60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    store._active_jobs[stale_id]["started_at"] = stale_at
    store._active_jobs[stale_id]["last_progress_at"] = stale_at

    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr("app.api.data.invalidate_job_status_cache", lambda: None)
    called = []

    daily_pipeline._run_tracked(
        lambda *, on_progress: called.append(True) or {"instruments_rows": 1},
        "daily_pipeline",
    )

    jobs_by_id = {job["id"]: job for job in store.list_recent(limit=2)}
    assert called == []
    stale_job = jobs_by_id[stale_id]
    blocked_job = next(job for job_id, job in jobs_by_id.items() if job_id != stale_id)
    assert blocked_job["status"] == "failed"
    assert "占用执行槽" in blocked_job["error"]
    assert stale_job["status"] == "failed"
    assert "无进度" in stale_job["error"]
    assert store.execution_owner() == stale_id
    store.release(stale_id, stale_owner)
