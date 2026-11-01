from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api.watchlist import _added_base_closes
from app.backtest.engine import BacktestEngine
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    EnrichedPublication,
    bump_enriched_generation,
    get_enriched_generation_snapshot,
)
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 29)


def frame(day=DAY, close=10.0):
    return pl.DataFrame({
        "symbol": ["600000.SH"], "date": [day],
        "open": [close], "high": [close], "low": [close],
        "close": [close], "volume": [1000.0],
    })


@pytest.fixture
def repo(tmp_path):
    result = KlineRepository(DataStore(tmp_path))
    yield result
    result.db.close()


def warm_history(repo):
    history = pl.concat([frame(), frame(DAY + timedelta(days=1), 11.0)])
    repo.append_enriched(history)
    # Install a completed warm snapshot, without unrelated indicator computation.
    repo._enriched_history_cache = history
    repo._enriched_history_start = DAY
    repo._enriched_cache_date = DAY + timedelta(days=1)
    repo._enriched_history_generation = repo.get_matrix_data_generation()
    if hasattr(repo, "get_enriched_generation_snapshot"):
        repo._enriched_history_snapshot = (
            history, repo.get_enriched_generation_snapshot("stock"),
        )
    return history


def test_live_tick_reuses_older_history_and_watchlist_baseline(repo):
    warm_history(repo)
    repo.flush_live_enriched(frame(DAY + timedelta(days=1), 12.0))
    actual = repo.get_enriched_range(DAY, DAY)
    assert actual is not None
    assert actual["close"].to_list() == [10.0]
    baseline = _added_base_closes(repo, {"600000.SH": DAY}, {"600000.SH"})
    assert baseline.height == 1
    # The warm snapshot contains yesterday's value for the modified day.
    assert repo.get_enriched_range(DAY, DAY + timedelta(days=1)) is None


def test_repository_refresh_records_a_scoped_snapshot(repo):
    history = pl.concat([frame(DAY + timedelta(days=i), 10.0 + i / 10) for i in range(5)])
    repo.append_enriched(history)
    assert repo.refresh_view("kline_enriched", repo._enriched_glob)
    repo._refresh_enriched()
    before = repo.get_enriched_range(DAY, DAY)
    assert before is not None
    repo.flush_live_enriched(frame(DAY + timedelta(days=4), 12.0))
    after = repo.get_enriched_range(DAY, DAY)
    assert after is not None
    assert after.equals(before)


def test_history_revision_and_unknown_changes_invalidate(repo):
    warm_history(repo)
    repo.flush_live_enriched(frame(DAY, 9.0))
    assert repo.get_enriched_range(DAY, DAY) is None
    warm_history(repo)
    bump_enriched_generation(repo.store.data_dir)
    assert repo.get_enriched_range(DAY, DAY) is None


@pytest.mark.parametrize("asset_type", ["stock", "etf"])
def test_panel_reuses_history_across_ticks_but_reloads_revisions(repo, asset_type):
    repo.flush_live_enriched_asset(asset_type, frame())
    engine = BacktestEngine(repo)
    # Exercise the real panel loader and its cache against persisted partitions.
    args = (None, DAY, DAY, ["symbol", "date", "close"], asset_type)
    first = engine.load_panel(*args)
    for offset in (1, 1, 2):
        repo.flush_live_enriched_asset(asset_type, frame(DAY + timedelta(days=offset), 12.0 + offset))
        assert engine.load_panel(*args).equals(first)
    assert engine._cache.stats()["compute_count"] == 1
    repo.flush_live_enriched_asset(asset_type, frame(DAY, 9.0))
    assert engine.load_panel(*args)["close"].to_list() == [9.0]
    assert engine._cache.stats()["compute_count"] == 2


def test_partial_publication_never_serves_warm_history(repo):
    warm_history(repo)
    publication = EnrichedPublication(repo.store.data_dir)
    out = repo.store.data_dir / "kline_daily_enriched/date=2026-10-30/part.parquet"
    publication.write_parquet(frame(DAY + timedelta(days=1), 12.0), out)
    assert repo.get_enriched_range(DAY, DAY) is None
    with pytest.raises(EnrichedGenerationUnavailableError):
        BacktestEngine(repo).load_panel(None, DAY, DAY)
    publication.abandon()


def test_matrix_cache_respects_coverage_and_historical_corrections(repo, monkeypatch):
    from app.backtest.engine import settings

    monkeypatch.setattr(settings, "backtest_matrix_disk_cache_enabled", True)
    repo.append_enriched(pl.concat([frame(), frame(DAY + timedelta(days=1), 11.0)]))
    engine = BacktestEngine(repo)
    plan = SimpleNamespace(
        execution_backend="matrix_native", base_columns={"close"},
        instrument_columns=set(), matrix_columns=set(),
    )

    def load(end=DAY):
        return engine.load_market_data_matrix_for_backtest(None, DAY, DAY, plan, coverage_end=end)

    assert load().cache_status == "built"
    repo.flush_live_enriched(frame(DAY + timedelta(days=1), 12.0))
    assert load().cache_status == "exact"
    # A requested wider cache must also be invalidated by writes in its coverage.
    load(DAY + timedelta(days=1))
    repo.flush_live_enriched(frame(DAY, 9.0))
    corrected = load()
    assert corrected.cache_status == "built"
    assert corrected.close[0, 0] == 9.0


def test_prefix_versions_survive_restart_noop_and_abandon(repo):
    repo.flush_live_enriched(frame())
    old = get_enriched_generation_snapshot(repo.store.data_dir)
    repo.flush_live_enriched(frame(DAY + timedelta(days=1), 12.0))
    current = get_enriched_generation_snapshot(repo.store.data_dir)
    assert old.through(DAY) == current.through(DAY)
    assert old.generation != current.generation
    repo.flush_live_enriched(frame(DAY + timedelta(days=1), 12.0))
    empty = EnrichedPublication(repo.store.data_dir)
    empty.begin()
    empty.abandon()
    assert get_enriched_generation_snapshot(repo.store.data_dir) == current
    restarted = KlineRepository(DataStore(repo.store.data_dir))
    try:
        assert restarted.get_enriched_generation_snapshot() == current
    finally:
        restarted.db.close()


def test_range_metadata_is_bounded_and_unknown_changes_reset_it(repo):
    from app.enriched_generation import _MAX_RANGE_GENERATIONS

    for offset in range(_MAX_RANGE_GENERATIONS + 2):
        repo.flush_live_enriched(frame(DAY + timedelta(days=offset)))
    snapshot = repo.get_enriched_generation_snapshot()
    assert len(snapshot.ranges) == _MAX_RANGE_GENERATIONS
    retained_day = DAY + timedelta(days=2)
    token = snapshot.through(retained_day)
    repo.flush_live_enriched(frame(DAY + timedelta(days=_MAX_RANGE_GENERATIONS + 3)))
    assert repo.get_enriched_generation_snapshot().through(retained_day) == token
    publication = EnrichedPublication(repo.store.data_dir)
    publication.begin()
    publication.mark_changed()
    publication.commit()
    assert repo.get_enriched_generation_snapshot().through(retained_day) != token


def test_correction_before_query_start_invalidates_indicator_dependencies(repo):
    repo.append_enriched(pl.concat([frame(), frame(DAY + timedelta(days=1), 11.0)]))
    before = repo.get_enriched_generation_snapshot()
    repo.flush_live_enriched(frame(DAY, 9.0))
    after = repo.get_enriched_generation_snapshot()
    # A rolling feature on D+1 may depend on D even when D is outside the query.
    assert before.through(DAY + timedelta(days=1)) != after.through(DAY + timedelta(days=1))


def test_legacy_writer_and_failed_recovery_cannot_reuse_scoped_versions(repo):
    import json

    warm_history(repo)
    old = repo.get_enriched_generation_snapshot()
    marker = repo.store.data_dir / ".matrix_generation_stock.json"
    marker.write_text(json.dumps({"state": "ready", "generation": "legacy-writer"}))
    assert repo.get_enriched_range(DAY, DAY) is None
    assert repo.get_enriched_generation_snapshot().through(DAY) == "legacy-writer"
    incomplete = EnrichedPublication(repo.store.data_dir)
    incomplete.write_parquet(frame(), repo.store.data_dir / "kline_daily_enriched/date=2026-10-29/part.parquet")
    incomplete.abandon()
    recovery = EnrichedPublication(repo.store.data_dir, recover=True)
    recovery.begin()
    recovery.abandon()
    assert repo.get_enriched_generation_snapshot().through(DAY) not in {old.through(DAY), "legacy-writer"}
