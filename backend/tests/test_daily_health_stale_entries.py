"""非交易日误报条目导致的 realtime_gate 409 死锁回归测试。

事故: 2026-11-08 (周日) 被写成 missing 条目; 休市日修补永远拉不到日K,
条目无法完成, assert_usable 无限抛 409, 看板加载失败。
修复: 状态层识别「确证非交易日的 missing 条目」并放行/清理,
      对 snapshot/corrupt/invalid 等真实缺陷保持原有严格门禁。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.services import daily_health as dh
from app.services import data_integrity as di


def _write_state(root: Path, entries: list[dict], revisions: dict | None = None) -> None:
    path = root / "user_data" / "daily_integrity_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "entries": entries,
        "revisions": revisions or {"stock": "aaa"},
    }, ensure_ascii=False), encoding="utf-8")
    dh._read.cache_clear()


def _entry(day: str, kind: str = "missing", symbol: str | None = None) -> dict:
    return {
        "asset_type": "stock", "day": day, "symbol": symbol,
        "needs_raw": True, "needs_enriched": True,
        "kind": kind, "reason": "test",
    }


SUNDAY = "2026-11-08"          # 实测 date(2026,11,8).weekday() == 6
TUESDAY = "2026-11-10"         # 实测交易日


# --- 日历判定 ---------------------------------------------------------------

def test_weekend_is_always_non_trading(monkeypatch):
    """周末零成本判定, 不依赖日历探测, 不受日历不可用影响。"""
    monkeypatch.setattr(di, "_trading_calendar", lambda: None)
    assert di.is_confirmed_non_trading_day(date(2026, 11, 8)) is True
    assert di.is_confirmed_non_trading_day(date(2026, 11, 7)) is True


def test_weekday_in_calendar_is_trading(monkeypatch):
    monkeypatch.setattr(di, "_trading_calendar", lambda: {date(2026, 11, 10)})
    assert di.is_confirmed_non_trading_day(date(2026, 11, 10)) is False


def test_weekday_holiday_is_non_trading(monkeypatch):
    """工作日但权威日历不含 → 节假日, 可确证休市。"""
    monkeypatch.setattr(di, "_trading_calendar", lambda: {date(2026, 11, 10)})
    assert di.is_confirmed_non_trading_day(date(2026, 11, 9)) is True


def test_calendar_unavailable_is_fail_closed(monkeypatch):
    """日历拿不到时, 工作日一律不判休市 —— 不能把真缺失当休市放过。"""
    monkeypatch.setattr(di, "_trading_calendar", lambda: None)
    assert di.is_confirmed_non_trading_day(date(2026, 11, 9)) is False


# --- 条目判定 ---------------------------------------------------------------

def test_only_missing_entries_are_stale(monkeypatch):
    monkeypatch.setattr(di, "is_confirmed_non_trading_day", lambda d: True)
    assert dh._is_stale_non_trading_entry(_entry(SUNDAY, "missing")) is True
    for kind in ("snapshot", "corrupt", "invalid"):
        assert dh._is_stale_non_trading_entry(_entry(SUNDAY, kind)) is False


def test_malformed_day_is_never_stale():
    assert dh._is_stale_non_trading_entry(_entry("not-a-date")) is False


# --- 门禁行为 ---------------------------------------------------------------

def test_assert_usable_ignores_stale_non_trading_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "is_confirmed_non_trading_day", lambda d: d.weekday() >= 5)
    _write_state(tmp_path, [_entry(SUNDAY)])
    dh.assert_usable(tmp_path)  # 不应抛错


def test_assert_usable_still_blocks_real_defect(tmp_path, monkeypatch):
    """真实缺陷条目必须继续拦, 修复不能变成整体放行。"""
    monkeypatch.setattr(di, "is_confirmed_non_trading_day", lambda d: d.weekday() >= 5)
    _write_state(tmp_path, [_entry(SUNDAY), _entry(TUESDAY, "snapshot", "000430.SZ")])
    with pytest.raises(dh.DailyIntegrityError) as exc:
        dh.assert_usable(tmp_path)
    assert TUESDAY in str(exc.value)


def test_assert_usable_still_blocks_trading_day_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "is_confirmed_non_trading_day", lambda d: d.weekday() >= 5)
    _write_state(tmp_path, [_entry(TUESDAY, "missing")])
    with pytest.raises(dh.DailyIntegrityError):
        dh.assert_usable(tmp_path)


# --- 清理行为 ---------------------------------------------------------------

def test_purge_removes_only_stale_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "is_confirmed_non_trading_day", lambda d: d.weekday() >= 5)
    _write_state(tmp_path, [
        _entry(SUNDAY),
        _entry(TUESDAY, "snapshot", "000430.SZ"),
        _entry(TUESDAY, "missing"),
    ])
    assert dh.purge_stale_non_trading_entries(tmp_path) == 1
    days = [(e["day"], e["kind"]) for e in dh.snapshot(tmp_path)["entries"]]
    assert (SUNDAY, "missing") not in days
    assert (TUESDAY, "snapshot") in days
    assert (TUESDAY, "missing") in days
    assert dh.purge_stale_non_trading_entries(tmp_path) == 0  # 幂等


def test_purge_updates_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "is_confirmed_non_trading_day", lambda d: d.weekday() >= 5)
    _write_state(tmp_path, [_entry(SUNDAY)], revisions={"stock": "before"})
    dh.purge_stale_non_trading_entries(tmp_path)
    assert dh.revision(tmp_path, "stock") != "before"


def test_record_scan_does_not_resurrect_stale_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "is_confirmed_non_trading_day", lambda d: d.weekday() >= 5)
    _write_state(tmp_path, [_entry(SUNDAY)])
    dh.record_scan(tmp_path, [], assets=["stock"],
                   start=date(2026, 11, 1), end=date(2026, 11, 11))
    assert dh.snapshot(tmp_path)["entries"] == []


# --- 扫描层: 休市日残留分区不得制造假候选 --------------------------------

def test_scan_ignores_residual_weekend_partition(tmp_path, monkeypatch):
    """回归 2026-11-08: 指数/日K 在周日留下残留分区, 曾让扫描把周日
    并进候选交易日, 生成永远修补不掉的 missing 条目并锁死门禁。
    """
    monkeypatch.setattr(di, "_trading_calendar", lambda: {date(2026, 11, 10)})
    for day in ("2026-11-08", "2026-11-10"):
        (tmp_path / "kline_daily" / f"date={day}").mkdir(parents=True)
    issues = di.scan_recent_integrity(
        tmp_path, today=date(2026, 11, 11), lookback_days=10,
        tables=("kline_daily",), deep=True,
    )
    assert all(issue.day != date(2026, 11, 8) for issue in issues)
