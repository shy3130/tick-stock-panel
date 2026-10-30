from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import data_integrity

TODAY = date(2026, 10, 30)
DAYS = {date(2026, 10, d) for d in (23, 26, 27, 28, 29, 30)}


def write_day(root, day, *, table="kline_daily", close=10.0):
    part = root / table / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600001.SH"], "date": [day],
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [close],
        "volume": [100.0], "amount": [100000.0],
        "quote_ts": pl.Series([None], dtype=pl.Int64),
    }).write_parquet(part / "part.parquet")
    return part / "part.parquet"


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: DAYS)


def test_recent_scan_finds_interior_missing_day(tmp_path):
    write_day(tmp_path, date(2026, 10, 27))
    write_day(tmp_path, date(2026, 10, 29))
    issues = data_integrity.scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.kind) for i in issues] == [(date(2026, 10, 28), "missing")]


def test_broken_parquet_is_not_accepted_as_clean(tmp_path):
    path = write_day(tmp_path, date(2026, 10, 29))
    path.write_bytes(b"truncated parquet")
    issues = data_integrity.scan_recent_integrity(tmp_path, today=TODAY)
    assert len(issues) == 1 and issues[0].kind == "corrupt"


def test_missing_day_forces_full_market_repair():
    issues = [
        data_integrity.IntegrityIssue(date(2026, 10, 27), "kline_daily", "snapshot", ("600001.SH",)),
        data_integrity.IntegrityIssue(date(2026, 10, 28), "kline_daily", "missing"),
    ]
    assert data_integrity._stock_snapshot_symbols(issues) is None


def test_deep_scan_reports_invalid_prices_and_empty_partition(tmp_path):
    write_day(tmp_path, date(2026, 10, 27), close=30.0)
    (tmp_path / "kline_daily/date=2026-10-28").mkdir()
    write_day(tmp_path, date(2026, 10, 29))
    issues = data_integrity.scan_recent_integrity(tmp_path, today=TODAY, deep=True)
    assert {(i.day, i.kind) for i in issues} == {
        (date(2026, 10, 27), "invalid"), (date(2026, 10, 28), "corrupt"),
    }


def test_manual_scan_does_not_label_unknown_holiday_as_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: None)
    write_day(tmp_path, date(2026, 10, 27))
    write_day(tmp_path, date(2026, 10, 29))
    issues = data_integrity.scan_recent_integrity(tmp_path, today=TODAY, deep=True)
    assert len(issues) == 1 and issues[0].kind == "unconfirmed"


def test_manual_scan_can_confirm_trading_day_from_another_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: None)
    write_day(tmp_path, date(2026, 10, 27))
    write_day(tmp_path, date(2026, 10, 29))
    write_day(tmp_path, date(2026, 10, 28), table="kline_etf_daily")
    issues = data_integrity.scan_recent_integrity(
        tmp_path, today=TODAY, deep=True, tables=("kline_daily",),
    )
    assert len(issues) == 1 and issues[0].kind == "missing"


def test_scan_ignores_today_in_progress(tmp_path):
    write_day(tmp_path, date(2026, 10, 29))
    write_day(tmp_path, TODAY, close=40.0)
    assert data_integrity.scan_recent_integrity(tmp_path, today=TODAY, deep=True) == []


@pytest.mark.parametrize("close,expected", [(10.0, []), (float("nan"), ["invalid"])])
def test_halted_rows_allow_zero_prices_but_reject_nonfinite_values(tmp_path, close, expected):
    path = write_day(tmp_path, date(2026, 10, 29))
    pl.read_parquet(path).with_columns(
        pl.lit(0.0).alias("volume"), pl.lit(0.0).alias("open"),
        pl.lit(0.0).alias("high"), pl.lit(0.0).alias("low"), pl.lit(close).alias("close"),
    ).write_parquet(path)
    issues = data_integrity.scan_recent_integrity(tmp_path, today=TODAY, deep=True)
    assert [i.kind for i in issues] == expected


def test_report_persists_and_repair_rechecks_with_backup(tmp_path, monkeypatch):
    from app.services import integrity_check, kline_sync
    from app.tickflow.repository import DataStore, KlineRepository

    monkeypatch.setattr(integrity_check, "cn_today", lambda: TODAY)
    path = write_day(tmp_path, date(2026, 10, 29))
    original = pl.read_parquet(path)
    path.write_bytes(b"broken original")
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(repo, "get_instruments_asset", lambda asset: original.select("symbol"))
    report = integrity_check.run_check(tmp_path, lookback_days=7, asset_types=["stock"])
    assert report["repairable_count"] == 1
    assert integrity_check.latest_report(tmp_path)["id"] == report["id"]
    calls = []

    def fetch(symbols, capset, **kwargs):
        calls.append((kwargs["start_time"].date(), kwargs["asset_type"]))
        assert path.read_bytes() == b"broken original"
        yield original

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    repaired = integrity_check.run_repair(repo, object(), report["id"])
    assert calls == [(date(2026, 10, 29), "stock")]
    assert repaired["issues"] == [] and repaired["status"] == "clean"
    backups = list((tmp_path / "integrity_backups").rglob("*.parquet"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"broken original"


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_repair_restores_original_and_reports_remaining(tmp_path, monkeypatch, cancel):
    from app.services import integrity_check, kline_sync
    from app.services.pipeline_jobs import JobCancelledError
    from app.tickflow.repository import DataStore, KlineRepository

    monkeypatch.setattr(integrity_check, "cn_today", lambda: TODAY)
    path = write_day(tmp_path, date(2026, 10, 29))
    healthy = path.with_name("healthy.parquet")
    healthy.write_bytes(path.read_bytes())
    path.write_bytes(b"broken original")
    report = integrity_check.run_check(tmp_path, lookback_days=7, asset_types=["stock"])

    failure = JobCancelledError("cancelled") if cancel else RuntimeError("source unavailable")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fail)
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(repo, "get_instruments_asset", lambda asset: pl.DataFrame({"symbol": ["600001.SH"]}))
    with pytest.raises(type(failure)):
        integrity_check.run_repair(repo, object(), report["id"])
    assert path.read_bytes() == b"broken original"
    assert healthy.is_file() and pl.read_parquet(healthy).height == 1
    final = integrity_check.latest_report(tmp_path)
    assert final["repair_error"] == str(failure)
    assert final["repairable_count"] == 1


def test_repair_refuses_stale_report(tmp_path, monkeypatch):
    from app.services import integrity_check

    monkeypatch.setattr(integrity_check, "cn_today", lambda: TODAY)
    write_day(tmp_path, date(2026, 10, 29))
    report = integrity_check.run_check(tmp_path, lookback_days=7, asset_types=["stock"])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(ValueError, match="重新自检"):
        integrity_check.run_repair(repo, object(), report["id"] + "old")


def test_scan_rejects_outside_symlink(tmp_path):
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"do not touch")
    data = tmp_path / "data"
    part = data / "kline_daily/date=2026-10-29"
    part.mkdir(parents=True)
    (part / "part.parquet").symlink_to(outside)
    issues = data_integrity.scan_recent_integrity(data, today=TODAY, deep=True)
    assert issues and "目录" in issues[0].reason
    assert outside.read_bytes() == b"do not touch"


@pytest.mark.parametrize("linked_table", [False, True])
def test_repair_refuses_linked_daily_directories(tmp_path, monkeypatch, linked_table):
    from app.services import integrity_check, kline_sync
    from app.tickflow.repository import DataStore, KlineRepository

    monkeypatch.setattr(integrity_check, "cn_today", lambda: TODAY)
    target = tmp_path / "linked"
    part = target / "date=2026-10-29" if linked_table else target
    part.mkdir(parents=True)
    original = part / "part.parquet"
    original.write_bytes(b"keep original")
    daily = tmp_path / "kline_daily"
    if linked_table:
        daily.symlink_to(target, target_is_directory=True)
    else:
        daily.mkdir()
        (daily / "date=2026-10-29").symlink_to(target, target_is_directory=True)
    report = integrity_check.run_check(tmp_path, asset_types=["stock"])
    calls = []
    monkeypatch.setattr(kline_sync, "iter_daily_frames", lambda *a, **kw: calls.append(True))
    repo = KlineRepository(DataStore(tmp_path))
    with pytest.raises(ValueError, match="包含链接"):
        integrity_check.run_repair(repo, object(), report["id"])
    assert not calls and original.read_bytes() == b"keep original"
