"""日K修补的业务边界: 精确取数、先校验、完整历史计算、隔离其他股票。"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import data_integrity, integrity_check, kline_sync
from app.tickflow.repository import DataStore, KlineRepository

A, B = "600001.SH", "600002.SH"
TODAY = date(2026, 10, 30)
BAD_DAY = date(2026, 10, 27)


def frame(day, symbols=(A, B), prices=(10.0, 20.0)):
    return pl.DataFrame({
        "symbol": list(symbols), "date": [day] * len(symbols),
        "open": list(prices), "high": [x + 1 for x in prices],
        "low": [x - 1 for x in prices], "close": list(prices),
        "volume": [100.0] * len(symbols), "amount": [1000.0] * len(symbols),
    })


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.jobs import daily_pipeline
    from app.services import preferences

    monkeypatch.setattr(integrity_check, "cn_today", lambda: TODAY)
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: {
        date(2026, 10, d) for d in (26, 27, 28, 29, 30)
    })
    monkeypatch.setattr(preferences, "load", lambda: {})
    def full_pipeline(*a, **kw):
        raise AssertionError("精准修补不应调用整条盘后管道")
    monkeypatch.setattr(daily_pipeline, "run_now", full_pipeline)
    repo = KlineRepository(DataStore(tmp_path))
    for n in range(4):
        day = BAD_DAY + timedelta(days=n)
        repo.append_daily(frame(day))
        repo.append_enriched(frame(day).with_columns(pl.col("close").alias("raw_close")))
    bad = tmp_path / "kline_daily" / f"date={BAD_DAY}" / "part.parquet"
    pl.read_parquet(bad).with_columns(
        pl.when(pl.col("symbol") == A).then(pl.lit(999.0)).otherwise(pl.col("close")).alias("close"),
    ).write_parquet(bad)
    calls = []

    def fetch(symbols, capset, *, start_time, end_time, asset_type="stock", on_chunk_done=None):
        calls.append((list(symbols), start_time.date(), end_time.date(), asset_type))
        # 源即使多返回股票/日期, 也只能改请求范围里的异常记录。
        yield pl.concat([frame(BAD_DAY), frame(BAD_DAY + timedelta(days=1), prices=(50.0, 60.0))])

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch, raising=False)
    return SimpleNamespace(root=tmp_path, repo=repo, caps=SimpleNamespace(has=lambda _: True), calls=calls)


def test_self_check_identifies_only_the_bad_symbol(env):
    report = integrity_check.run_check(env.root, asset_types=["stock"])
    assert report["issues"][0]["symbols"] == [A]
    assert report["issues"][0]["symbol_count"] == 1


def test_repair_fetches_only_bad_symbol_and_day_and_preserves_other_rows(env):
    report = integrity_check.run_check(env.root, asset_types=["stock"])
    next_day = env.root / "kline_daily" / f"date={BAD_DAY + timedelta(days=1)}" / "part.parquet"
    next_before = next_day.read_bytes()
    other_before = pl.read_parquet(env.root / f"kline_daily/date={BAD_DAY}/part.parquet").filter(pl.col("symbol") == B)
    result = integrity_check.run_repair(env.repo, env.caps, report["id"])
    assert env.calls == [([A], BAD_DAY, BAD_DAY, "stock")]
    assert next_day.read_bytes() == next_before
    repaired = pl.read_parquet(env.root / f"kline_daily/date={BAD_DAY}/part.parquet")
    assert repaired.filter(pl.col("symbol") == B).equals(other_before)
    assert repaired.filter(pl.col("symbol") == A)["close"].item() == 10.0
    assert result["status"] == "clean"


@pytest.mark.parametrize("empty", [False, True])
def test_failed_source_validation_never_changes_raw_or_enriched(env, monkeypatch, empty):
    from app.indicators import pipeline

    report = integrity_check.run_check(env.root, asset_types=["stock"])
    before = {p: p.read_bytes() for name in ("kline_daily", "kline_daily_enriched")
              for p in (env.root / name).rglob("*.parquet")}
    computed = []
    monkeypatch.setattr(pipeline, "recompute_daily_symbols", lambda *a, **kw: computed.append(True), raising=False)

    def fetch(*a, **kw):
        yield pl.DataFrame() if empty else frame(BAD_DAY, (A,), (float("nan"),))

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    with pytest.raises((ValueError, RuntimeError)):
        integrity_check.run_repair(env.repo, env.caps, report["id"])
    assert not computed
    assert all(p.read_bytes() == value for p, value in before.items())


def test_one_corrupt_file_falls_back_to_only_its_day(env):
    path = env.root / f"kline_daily/date={BAD_DAY}/part.parquet"
    path.write_bytes(b"unreadable whole day")
    report = integrity_check.run_check(env.root, asset_types=["stock"])
    result = integrity_check.run_repair(env.repo, env.caps, report["id"])
    assert env.calls == [([A, B], BAD_DAY, BAD_DAY, "stock")]
    assert result["status"] == "clean"
    backups = list((env.root / "integrity_backups").rglob("*.parquet"))
    assert backups[0].read_bytes() == b"unreadable whole day"


@pytest.mark.parametrize("cancel", [False, True])
def test_retry_after_recompute_failure_does_not_fetch_day_k_again(env, monkeypatch, cancel):
    from app.indicators import pipeline
    from app.services.daily_health import DailyIntegrityError, assert_usable
    from app.services.pipeline_jobs import JobCancelledError

    original = pipeline.recompute_daily_symbols
    report = integrity_check.run_check(env.root, asset_types=["stock"])
    failure = JobCancelledError("cancelled") if cancel else RuntimeError("compute failed")

    def fail(*a, **kw):
        raise failure

    monkeypatch.setattr(pipeline, "recompute_daily_symbols", fail)
    with pytest.raises(type(failure)):
        integrity_check.run_repair(env.repo, env.caps, report["id"])
    pending = integrity_check.latest_report(env.root)
    assert pending["issues"][0]["kind"] == "recompute"
    assert pending["repairable_count"] == 1
    assert len(env.calls) == 1
    with pytest.raises(DailyIntegrityError):
        assert_usable(env.root, symbols=[A])
    monkeypatch.setattr(pipeline, "recompute_daily_symbols", original)
    result = integrity_check.run_repair(env.repo, env.caps, pending["id"])
    assert result["status"] == "clean" and len(env.calls) == 1
    assert_usable(env.root)


def test_known_bad_data_blocks_dependents_but_not_other_stock_or_etf(env, monkeypatch):
    from app.backtest.engine import BacktestEngine
    from app.services import daily_health
    from app.services.screener import ScreenerService

    integrity_check.run_check(env.root, asset_types=["stock"])
    with pytest.raises(daily_health.DailyIntegrityError):
        env.repo.get_daily(A, BAD_DAY, TODAY, columns=["close"])
    other = env.repo.get_daily(B, BAD_DAY, TODAY, columns=["symbol", "date", "close"])
    assert other.height == 4 and set(other["close"].to_list()) == {20.0}
    daily_health.assert_usable(env.root, "etf")
    with pytest.raises(daily_health.DailyIntegrityError):
        BacktestEngine(env.repo).data_generation_await(timeout_s=0.01)
    with pytest.raises(daily_health.DailyIntegrityError):
        ScreenerService(env.repo).run(TODAY, ["close > 0"])


def test_scanning_another_asset_cannot_erase_known_pending_stock(env):
    from app.services import daily_health

    integrity_check.run_check(env.root, asset_types=["stock"])
    integrity_check.run_check(env.root, lookback_days=1, asset_types=["etf"])
    with pytest.raises(daily_health.DailyIntegrityError):
        daily_health.assert_usable(env.root)
    report = integrity_check.run_check(env.root, lookback_days=1, asset_types=["stock"])
    assert report["repairable_count"] == 1
    assert report["issues"][0]["date"] == BAD_DAY.isoformat()


def test_targeted_repair_keeps_unhandled_issue_and_does_not_recompute_its_stock(env, monkeypatch):
    from app.indicators import pipeline
    from app.services import daily_health, repair_daily

    issue = data_integrity.IntegrityIssue(BAD_DAY, "kline_daily", "invalid", symbols=(A,))
    other_day = BAD_DAY + timedelta(days=1)
    other = data_integrity.IntegrityIssue(other_day, "kline_daily", "corrupt", symbols=(B,),
                                        reason="文件位于数据目录以外")
    daily_health.record_scan(env.root, [issue, other], assets=["stock"], start=BAD_DAY, end=other_day)
    computed = []
    monkeypatch.setattr(pipeline, "recompute_daily_symbols",
                        lambda repo, asset, **kw: computed.append((asset, kw["start"], kw["symbols"])))
    monkeypatch.setattr(repair_daily, "_refresh_repair_dependents", lambda *args: None)
    repair_daily.repair_integrity_issues(env.repo, env.caps, [issue],
                                       backup_dir=env.root / "integrity_backups" / "targeted")
    assert computed == [("stock", BAD_DAY, [A])]
    remaining = daily_health.snapshot(env.root)["entries"]
    assert len(remaining) == 1
    assert remaining[0]["symbol"] == B and remaining[0]["needs_raw"]
    assert remaining[0]["needs_enriched"]


@pytest.mark.parametrize("table", ["kline_daily", "kline_daily_enriched"])
def test_repair_rejects_linked_history_and_preserves_outside_file(env, table):
    from app.services import daily_health

    linked_day = BAD_DAY + timedelta(days=1) if table == "kline_daily" else BAD_DAY
    path = env.root / table / f"date={linked_day}/part.parquet"
    outside = env.root.parent / f"{env.root.name}-{table}-outside.parquet"
    original = path.read_bytes()
    outside.write_bytes(original)
    path.unlink()
    path.symlink_to(outside)
    report = integrity_check.run_check(env.root, asset_types=["stock"])
    with pytest.raises(ValueError, match=r"链接|目录以外"):
        integrity_check.run_repair(env.repo, env.caps, report["id"])
    assert path.is_symlink() and outside.read_bytes() == original
    with pytest.raises(daily_health.DailyIntegrityError):
        daily_health.assert_usable(env.root)


@pytest.mark.parametrize("asset,symbol", [("etf", "510300.SH"), ("index", "000001.SH")])
def test_etf_and_index_repairs_never_write_stock_data(env, monkeypatch, asset, symbol):
    from app.tickflow.repository import daily_dirname

    stock_before = {p: p.read_bytes() for p in (env.root / "kline_daily").rglob("*.parquet")}
    for n in range(3):
        env.repo.append_daily_asset(asset, frame(BAD_DAY + timedelta(days=n), (symbol,), (10.0,)))
    path = env.root / daily_dirname(asset) / f"date={BAD_DAY}/part.parquet"
    pl.read_parquet(path).with_columns(pl.lit(-1.0).alias("close")).write_parquet(path)
    calls = []

    def fetch(symbols, capset, **kwargs):
        calls.append((symbols, kwargs["asset_type"]))
        yield frame(BAD_DAY, (symbol,), (10.0,))

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    report = integrity_check.run_check(env.root, asset_types=[asset])
    assert integrity_check.run_repair(env.repo, env.caps, report["id"])["status"] == "clean"
    assert calls == [([symbol], asset)]
    assert all(p.read_bytes() == value for p, value in stock_before.items())


def test_recompute_uses_full_raw_history_and_preserves_prior_dates_and_other_stock(env, monkeypatch):
    from polars.testing import assert_frame_equal

    from app.indicators import pipeline

    days = []
    cursor = BAD_DAY - timedelta(days=150)
    while cursor < BAD_DAY:
        if cursor.weekday() < 5:
            days.append(cursor)
            env.repo.append_daily(frame(cursor, prices=(10 + len(days) % 11 / 10, 20.0)))
        cursor += timedelta(days=1)
    instruments = pl.DataFrame({"symbol": [A, B], "name": ["A", "B"], "float_shares": [1e6, 2e6]})
    instruments.write_parquet(env.root / "instruments/all.parquet")
    factors = pl.DataFrame({"symbol": [A], "trade_date": [days[30]], "ex_factor": [1.1]})
    factors.write_parquet(env.root / "adj_factor/all.parquet")
    shares = pl.DataFrame({"symbol": [A], "period_end": [days[0]], "announce_date": [days[20]], "float_shares": [800000.0]})
    shares.write_parquet(env.root / "financials/shares/part.parquet")
    monkeypatch.setattr(env.repo, "get_instruments_asset", lambda asset: instruments)
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: set(days) | {BAD_DAY + timedelta(days=n) for n in range(4)})
    raw = pl.concat([pl.read_parquet(p) for p in (env.root / "kline_daily").rglob("*.parquet")]).sort("symbol", "date")
    correct = raw.with_columns(pl.when((pl.col("symbol") == A) & (pl.col("date") == BAD_DAY))
                               .then(pl.lit(10.0)).otherwise(pl.col("close")).alias("close"))
    expected = pipeline._select_storage_cols(pipeline.compute_enriched(correct, factors=factors, instruments=instruments, historical_shares=shares))
    env.repo.append_enriched(expected)
    prior = env.root / f"kline_daily_enriched/date={days[-1]}/part.parquet"
    prior_bytes = prior.read_bytes()
    other_before = expected.filter(pl.col("symbol") == B).sort("date")
    report = integrity_check.run_check(env.root, asset_types=["stock"])
    result = integrity_check.run_repair(env.repo, env.caps, report["id"])
    assert result["status"] == "clean"
    from app.parquet import scan_enriched_parquet
    actual = scan_enriched_parquet(str(env.root / "kline_daily_enriched/date=*/*.parquet")).collect().select(expected.columns).sort("symbol", "date")
    assert_frame_equal(actual.filter(pl.col("symbol") == B).sort("date"), other_before, check_exact=True)
    assert_frame_equal(actual.filter(pl.col("symbol") == A), expected.filter(pl.col("symbol") == A).sort("date"), check_exact=True)
    assert prior.read_bytes() == prior_bytes
    indicators = pipeline.compute_indicators(actual.filter(pl.col("symbol") == A))
    full_expected = pipeline.compute_indicators(expected.filter(pl.col("symbol") == A))
    assert_frame_equal(indicators.select("date", "ma20", "macd_dif"), full_expected.select("date", "ma20", "macd_dif"), check_exact=True)


def test_interrupted_publication_retries_without_fetching_or_widening_scope(env, monkeypatch):
    from app.enriched_generation import EnrichedPublication
    from app.services.daily_health import DailyIntegrityError, assert_usable

    write = EnrichedPublication.write_parquet
    failed = False

    def fail_once(self, data, path):
        nonlocal failed
        if path.parent.name == f"date={BAD_DAY + timedelta(days=1)}" and not failed:
            failed = True
            raise OSError("write interrupted")
        return write(self, data, path)

    report = integrity_check.run_check(env.root, asset_types=["stock"])
    monkeypatch.setattr(EnrichedPublication, "write_parquet", fail_once)
    with pytest.raises(OSError, match="interrupted"):
        integrity_check.run_repair(env.repo, env.caps, report["id"])
    with pytest.raises(DailyIntegrityError):
        assert_usable(env.root)
    pending = integrity_check.latest_report(env.root)
    assert integrity_check.run_repair(env.repo, env.caps, pending["id"])["status"] == "clean"
    assert len(env.calls) == 1
    assert_usable(env.root)


def test_precise_repair_does_not_take_over_unrelated_incomplete_publication(env):
    import json

    from app.enriched_generation import EnrichedGenerationUnavailableError, EnrichedPublication

    previous = EnrichedPublication(env.root, recover=True)
    previous.write_parquet(frame(BAD_DAY, (B,), (20.0,)), env.root / f"kline_daily_enriched/date={BAD_DAY}/part.parquet")
    previous.abandon()
    report = integrity_check.run_check(env.root, asset_types=["stock"])
    with pytest.raises(EnrichedGenerationUnavailableError, match="其他未完成"):
        integrity_check.run_repair(env.repo, env.caps, report["id"])
    marker = json.loads((env.root / ".matrix_generation_stock.json").read_text())
    assert marker["state"] == "publishing"


def test_empty_recomputed_market_result_removes_old_rows_only_in_repaired_range(env):
    from app.services import market_mainline, regime_builder

    before = BAD_DAY - timedelta(days=1)
    path = market_mainline.mainline_path(env.root)
    path.parent.mkdir(parents=True)
    pl.DataFrame({"date": [before, BAD_DAY, BAD_DAY], "kind": ["concept", "concept", "industry"],
                  "rank": [1, 1, 1], "name": ["keep prior", "stale", "keep other kind"]}).write_parquet(path)
    market_mainline.replace_mainline_range(env.root, BAD_DAY, TODAY, "concept", pl.DataFrame())
    assert pl.read_parquet(path)["name"].to_list() == ["keep prior", "keep other kind"]
    regime = regime_builder.regime_path(env.root)
    regime.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"date": [before, BAD_DAY], "score": [50.0, 99.0]}).write_parquet(regime)
    regime_builder.replace_regime_range(env.root, BAD_DAY, TODAY, pl.DataFrame())
    assert pl.read_parquet(regime)["date"].to_list() == [before]


def test_completed_backtest_reconnect_cache_expires_after_daily_correction(env, monkeypatch):
    import time

    from app.api import backtest
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", env.root)
    job = backtest._BacktestJob("repair-test")
    job.done = True
    job.finish_ts = time.time()
    monkeypatch.setattr(backtest, "_running_jobs", {job.key: job})
    integrity_check.run_check(env.root, asset_types=["stock"])
    backtest._cleanup_stale_jobs()
    assert job.key not in backtest._running_jobs


def test_integrity_notifications_reach_every_subscriber_without_dropping_repair_event():
    from app.services.quote_service import QuoteService

    service = QuoteService()
    first, second = service.subscribe(), service.subscribe()
    service.notify_daily_integrity_updated(["stock"], data_changed=False)
    service.notify_daily_integrity_updated(["stock"], data_changed=True)
    for sub in (first, second):
        assert sub.wait(timeout=0.01)
        assert sub.pop()["daily_integrity_updated"] is True
        assert not sub.wait(timeout=0.01)


def test_realtime_matrix_rebuilds_history_after_daily_repair_revision(monkeypatch):
    import numpy as np

    from app.strategy.engine import StrategyDataContext, StrategyEngine

    engine = StrategyEngine(strategy_dirs=[])
    strategy = SimpleNamespace(meta={}, execution_backend="matrix_native",
                               matrix_strategy=SimpleNamespace(required_warmup_bars=lambda params: 2))
    monkeypatch.setattr(engine, "get", lambda sid: strategy)
    monkeypatch.setattr(engine, "resolve_params", lambda *args: {})
    monkeypatch.setattr(engine, "_matrix_field_columns", lambda *args: {"close"})
    current = frame(TODAY, (A,), (30.0,))
    old_history = pl.concat([frame(BAD_DAY, (A,), (10.0,)), frame(BAD_DAY + timedelta(days=1), (A,), (20.0,))])
    corrected = pl.concat([frame(BAD_DAY, (A,), (5.0,)), frame(BAD_DAY + timedelta(days=1), (A,), (7.0,))])
    first = engine.prepare_realtime_matrix(StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=TODAY, current=current,
        history=old_history, cache_key="monitor:stock", data_generation="before-repair",
    ), ["test"])
    second = engine.prepare_realtime_matrix(StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=TODAY, current=current,
        history=corrected, cache_key="monitor:stock", data_generation="after-repair",
    ), ["test"])
    np.testing.assert_allclose(first.close[:, 0], [10.0, 20.0, 30.0])
    np.testing.assert_allclose(second.close[:, 0], [5.0, 7.0, 30.0])
