"""分钟数据缺口体检 (services/minute_gaps) 与 /api/kline/minute 的 partial 返回。"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import polars as pl
import pytest

from app.services.minute_gaps import FULL_DAY_BARS, scan_minute_gaps

_BJ = _dt.date(2026, 10, 23)


def _write_partition(minute_dir: Path, trade_date: str, rows: list[tuple[str, str]]) -> None:
    """写一个 date=YYYY-MM-DD/part.parquet, rows 为 (symbol, 'HH:MM')。"""
    d = minute_dir / f"date={trade_date}"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "datetime": [_dt.datetime.fromisoformat(f"{trade_date}T{t}:00") for _, t in rows],
        "open": [10.0] * len(rows),
        "high": [10.0] * len(rows),
        "low": [10.0] * len(rows),
        "close": [10.0] * len(rows),
        "volume": [1.0] * len(rows),
        "amount": [1.0] * len(rows),
    }).write_parquet(d / "part.parquet")


def _full_day(symbol: str) -> list[tuple[str, str]]:
    """完整交易日分钟序列 (与实盘落盘同口径: 09:30~11:30 + 13:00~15:00, 241 根)。

    注意: 实盘分区常见 241 根 (两个端点都含), 完整性阈值是 expected*0.9,
    所以 241 根也判为完整 —— 与本服务的容差口径一致。
    """
    times: list[str] = []
    t = _dt.datetime(2026, 1, 1, 9, 30)
    while t <= _dt.datetime(2026, 1, 1, 11, 30):
        times.append(t.strftime("%H:%M"))
        t += _dt.timedelta(minutes=1)
    t = _dt.datetime(2026, 1, 1, 13, 0)
    while t <= _dt.datetime(2026, 1, 1, 15, 0):
        times.append(t.strftime("%H:%M"))
        t += _dt.timedelta(minutes=1)
    return [(symbol, hm) for hm in times]


def test_complete_day_reports_no_gap(tmp_path):
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, "2026-10-22", _full_day("600519.SH"))
    r = scan_minute_gaps(minute_dir, today=_BJ, now_hm=(20, 0))
    assert r["gaps"] == []
    assert r["complete_dates"] == ["2026-10-22"]


def test_partial_day_reported_with_time_range(tmp_path):
    minute_dir = tmp_path / "kline_minute"
    # 171 根, 截到 13:49 (复现 2026-10-20 的真实缺口形态: 午后中断)
    rows = _full_day("600519.SH")[:171]
    _write_partition(minute_dir, "2026-10-20", rows)
    r = scan_minute_gaps(minute_dir, today=_BJ, now_hm=(20, 0))
    assert len(r["gaps"]) == 1
    g = r["gaps"][0]
    assert g["symbol"] == "600519.SH"
    assert g["date"] == "2026-10-20"
    assert g["actual_bars"] == 171
    assert g["expected_bars"] == FULL_DAY_BARS
    assert g["missing_bars"] == 69
    assert g["last_time"] == "13:49"


def test_tail_only_sequence_also_reported(tmp_path):
    """只有尾盘一段 (复现 2026-10-21 的尾部残序列) 同样算缺口。"""
    minute_dir = tmp_path / "kline_minute"
    full = _full_day("002388.SZ")
    _write_partition(minute_dir, "2026-10-21", full[-23:])
    r = scan_minute_gaps(minute_dir, today=_BJ, now_hm=(20, 0))
    assert len(r["gaps"]) == 1
    assert r["gaps"][0]["first_time"] == "14:38"
    assert r["gaps"][0]["last_time"] == "15:00"


def test_symbol_filter_narrows_scan(tmp_path):
    minute_dir = tmp_path / "kline_minute"
    rows = _full_day("A.SZ")[:100] + _full_day("B.SZ")[:100]
    _write_partition(minute_dir, "2026-10-20", rows)
    r = scan_minute_gaps(minute_dir, today=_BJ, now_hm=(20, 0), symbols=["A.SZ"])
    assert {g["symbol"] for g in r["gaps"]} == {"A.SZ"}


def test_summary_by_date_aggregates_missing_bars(tmp_path):
    minute_dir = tmp_path / "kline_minute"
    rows = _full_day("A.SZ")[:171] + _full_day("B.SZ")[:120]
    _write_partition(minute_dir, "2026-10-20", rows)
    r = scan_minute_gaps(minute_dir, today=_BJ, now_hm=(20, 0))
    cell = r["summary_by_date"][0]
    assert cell["gap_symbols"] == 2
    assert cell["missing_bars"] == (240 - 171) + (240 - 120)


def test_intraday_expected_scales_with_clock(tmp_path):
    """当日盘中: expected 按已开盘分钟折算, 正常进度不应误报缺口。"""
    minute_dir = tmp_path / "kline_minute"
    today_str = "2026-10-23"
    rows = _full_day("600519.SH")[:90]   # 11:00 应有 90 根
    _write_partition(minute_dir, today_str, rows)
    r = scan_minute_gaps(minute_dir, today=_BJ, now_hm=(11, 0))
    assert r["gaps"] == []   # 90 >= 90*0.9, 属正常进度


def test_missing_dir_returns_empty(tmp_path):
    r = scan_minute_gaps(tmp_path / "nope", today=_BJ, now_hm=(20, 0))
    assert r["gaps"] == []
    assert r["scanned_dates"] == 0


# ---------- /api/kline/minute 的 local_partial 返回 ----------

def _fake_req(repo):
    state = type("S", (), {"repo": repo})()
    return type("R", (), {"app": type("A", (), {"state": state})()})()


def test_get_minute_returns_partial_instead_of_dropping(tmp_path, monkeypatch):
    """本地 171 根必须原样返回 (source=local_partial), 不能丢弃去问源。

    回归: 此前把不完整当无数据 → 补拉历史日期恒空 → 前端白屏。
    """
    from app.api import kline as kline_api
    from app.tickflow.capabilities import CapabilitySet
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    rows = _full_day("600519.SH")[:171]
    day_df = pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "datetime": [_dt.datetime.fromisoformat(f"2026-10-20T{t}:00") for _, t in rows],
        "open": [10.0] * len(rows), "high": [10.0] * len(rows),
        "low": [10.0] * len(rows), "close": [10.0] * len(rows),
        "volume": [1.0] * len(rows), "amount": [1.0] * len(rows),
    })
    monkeypatch.setattr(repo, "get_minute", lambda *a, **kw: day_df)
    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    monkeypatch.setattr(kline_api, "_get_stock_info", lambda *a, **kw: {"name": "测试"})
    monkeypatch.setattr(kline_api, "_get_previous_closes", lambda *a, **kw: {})
    monkeypatch.setattr(kline_api, "_get_price_limit_info", lambda *a, **kw: None)
    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ)
    monkeypatch.setattr(kline_api.minute_adjust, "minute_basis_is_raw", lambda d: False)

    # 补拉绝不能被调用 (否则等于又走回丢弃路径)
    called = {"n": 0}
    def _no_pull(*a, **kw):
        called["n"] += 1
        return pl.DataFrame()
    monkeypatch.setattr(kline_api.kline_sync, "fetch_minute_single", _no_pull)

    state = type("S", (), {"repo": repo, "capabilities": CapabilitySet()})()
    req = type("R", (), {"app": type("A", (), {"state": state}), "headers": {}})()

    out = kline_api.get_minute(req, symbol="600519.SH", trade_date=_dt.date(2026, 10, 20), live=False)

    assert out["source"] == "local_partial"
    assert len(out["rows"]) == 171
    assert out["expected"] == 240
    assert out["actual"] == 171
    assert called["n"] == 0, "有本地残缺数据时不应再向源补拉"
