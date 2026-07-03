from datetime import date
from types import SimpleNamespace

import polars as pl

from app.jobs import daily_pipeline
from app.jobs.daily_pipeline import _latest_enriched_date
from app.services import preferences
from app.capabilities import CapabilitySet


def test_latest_enriched_date_uses_partition_names(tmp_path):
    for ds in ("2026-07-01", "bad", "2026-07-02"):
        (tmp_path / "kline_daily_enriched" / f"date={ds}").mkdir(parents=True)

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    assert _latest_enriched_date(repo) == date(2026, 7, 2)


def test_run_now_local_mode_skips_raw_sync_and_runs_local_pipeline(tmp_path, monkeypatch):
    calls = {"local": 0, "raw": 0}
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: None,
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
    monkeypatch.setattr(preferences, "get_minute_sync_enabled", lambda: False)
    monkeypatch.setattr(preferences, "get_minute_sync_days", lambda: 5)
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_and_persist_daily_batch", lambda *a, **k: calls.__setitem__("raw", calls["raw"] + 1))
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: object())
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")

    def fake_local(*args, **kwargs):
        calls["local"] += 1
        return 7

    monkeypatch.setattr(daily_pipeline, "run_pipeline_local", fake_local)

    result = daily_pipeline.run_now(repo, CapabilitySet())

    assert calls == {"local": 1, "raw": 0}
    assert result["enriched_days"] == 7


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
    monkeypatch.setattr(daily_pipeline, "run_now", lambda *a, **k: calls.__setitem__("run", calls["run"] + 1))

    result = daily_pipeline.bootstrap_local_enriched_if_stale(repo, CapabilitySet())

    assert result["reason"] == "incomplete"
    assert calls["run"] == 0


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
    monkeypatch.setattr(daily_pipeline, "_local_partition_coverage_ok", lambda repo, previous, target: True)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset: ["600519.SH"])
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: object())
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
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
    monkeypatch.setattr(daily_pipeline, "_local_partition_coverage_ok", lambda repo, previous, target: False)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset: ["600519.SH"])
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: object())
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr(daily_pipeline, "run_pipeline_local_incremental", lambda *a, **k: 1)

    result = daily_pipeline.bootstrap_local_enriched_if_stale(repo, CapabilitySet())

    assert result["reason"] == "partition_incomplete"
    assert result["written"] == 1
    assert refreshed["ok"] is True


def test_local_daily_coverage_gate_requires_most_sample_symbols(monkeypatch):
    class Provider:
        def get_daily(self, symbols, start, end, asset_type):
            return pl.DataFrame({"symbol": ["A", "B"], "date": [date(2026, 7, 3), date(2026, 7, 3)]})

    monkeypatch.setattr("app.data_providers.get_provider", lambda name: Provider())
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")

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

    assert daily_pipeline._local_partition_coverage_ok(repo, date(2026, 7, 2), date(2026, 7, 3)) is False

    pl.DataFrame({"symbol": ["A", "B", "C", "D"]}).write_parquet(target / "part.parquet")
    assert daily_pipeline._local_partition_coverage_ok(repo, date(2026, 7, 2), date(2026, 7, 3)) is True
