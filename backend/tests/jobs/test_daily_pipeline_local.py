from datetime import date
from types import SimpleNamespace

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
