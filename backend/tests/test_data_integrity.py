"""数据完整性检测 (停机缺口/盘中快照) 单元测试。

判据核心: quote_ts 仅实时 flush 写入真实毫秒时间戳 (batch 拉取/盘后计算为
null); 历史交易日的 quote_ts 时刻 < 15:00 即盘中快照 → 坏。
"""
from __future__ import annotations

import time
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import app.data_providers
import polars as pl
import pytest

from app.market_time import CN_TZ, cn_today
from app.services import data_integrity as di
from app.services.data_integrity import (
    AUTO_REPAIR_MAX_LAG_DAYS,
    IntegrityIssue,
    _candidate_days,
    _is_snapshot,
    _quote_ts_max_ms,
    _trading_calendar,
    earliest_issue_day,
    prune_enriched_partitions,
    scan_recent_integrity,
    within_auto_repair_window,
)

# 2026-08-19(周三) ~ 2026-08-21(周五) 是工作日; TODAY 取 2026-08-24(周一)
TODAY = date(2026, 8, 24)
FRIDAY = date(2026, 8, 21)
THURSDAY = date(2026, 8, 20)


def _ts_ms(day: date, t: time) -> int:
    return int(datetime.combine(day, t, tzinfo=CN_TZ).timestamp() * 1000)


def _write_daily_partition(root, table: str, day: date, quote_ts: int | None, symbols=("600001.SH",)) -> None:
    part = root / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    n = len(symbols)
    pl.DataFrame({
        "symbol": list(symbols),
        "date": [day] * n,
        "open": [10.0] * n,
        "high": [10.1] * n,
        "low": [9.9] * n,
        "close": [10.0] * n,
        "volume": [100.0] * n,
        "amount": [1000.0] * n,
        "quote_ts": [quote_ts] * n,
    }).write_parquet(part / "part.parquet")


# ── 判据 ────────────────────────────────────────────────────────────


def test_snapshot_predicate():
    noon = _ts_ms(FRIDAY, time(11, 58))
    after_close = _ts_ms(FRIDAY, time(15, 0, 30))
    assert _is_snapshot(FRIDAY, noon) is True
    assert _is_snapshot(FRIDAY, after_close) is False
    assert _is_snapshot(FRIDAY, None) is False  # batch 历史 → 权威


def test_quote_ts_max_reads_partition_statistics(tmp_path):
    part = tmp_path / "date=2026-08-21"
    part.mkdir()
    pl.DataFrame({
        "symbol": ["a", "b"],
        "quote_ts": [1000, 2000],
    }).write_parquet(part / "part.parquet")
    assert _quote_ts_max_ms(part) == 2000


def test_quote_ts_max_none_for_all_null(tmp_path):
    part = tmp_path / "date=2026-08-21"
    part.mkdir()
    pl.DataFrame({
        "symbol": ["a", "b"],
        "quote_ts": [None, None],
    }).write_parquet(part / "part.parquet")
    assert _quote_ts_max_ms(part) is None


# ── 扫描 ────────────────────────────────────────────────────────────


def test_batch_history_with_null_quote_ts_is_clean(tmp_path):
    _write_daily_partition(tmp_path, "kline_daily", THURSDAY, None)
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, None)
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    # 周六/周日非工作日不扫; 无今日分区且周五为最新 → 周五之后无缺口
    assert issues == []


def test_midday_snapshot_partition_is_flagged(tmp_path):
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, _ts_ms(FRIDAY, time(11, 58)))
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.table, i.kind) for i in issues] == [
        (FRIDAY, "kline_daily", "snapshot"),
    ]


def test_final_snapshot_after_close_is_clean(tmp_path):
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, _ts_ms(FRIDAY, time(15, 1)))
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))
    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_zero_volume_live_residue_amid_batch_rows_is_clean(tmp_path):
    """盘后 batch 已覆盖主体数据时, 单个停牌实时残留不能误判整个分区。"""
    part = tmp_path / "kline_daily" / f"date={FRIDAY.isoformat()}"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        "date": [FRIDAY] * 4,
        "open": [10.0, 9.8, 12.0, 8.0],
        "high": [10.2, 9.8, 12.2, 8.1],
        "low": [9.9, 9.8, 11.9, 7.9],
        "close": [10.1, 9.8, 12.1, 8.0],
        "volume": [1000.0, 0.0, 1200.0, 800.0],
        "amount": [10100.0, 0.0, 14520.0, 6400.0],
        "quote_ts": [None, _ts_ms(FRIDAY, time(9, 15)), None, None],
    }).write_parquet(part / "part.parquet")
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))

    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_mostly_zero_preopen_rows_with_one_batch_row_is_flagged(tmp_path):
    """少量 batch 行不能掩盖占主体的盘前实时快照。"""
    part = tmp_path / "kline_daily" / f"date={FRIDAY.isoformat()}"
    part.mkdir(parents=True)
    preopen_ts = _ts_ms(FRIDAY, time(9, 15))
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        "date": [FRIDAY] * 4,
        "open": [10.0, 20.0, 30.0, 40.0],
        "high": [10.0, 20.0, 30.0, 40.1],
        "low": [10.0, 20.0, 30.0, 39.9],
        "close": [10.0, 20.0, 30.0, 40.0],
        "volume": [0.0, 0.0, 0.0, 100.0],
        "amount": [0.0, 0.0, 0.0, 4000.0],
        "quote_ts": [preopen_ts, preopen_ts, preopen_ts, None],
    }).write_parquet(part / "part.parquet")
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))

    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.table, i.kind) for i in issues] == [
        (FRIDAY, "kline_daily", "snapshot"),
    ]


def test_all_zero_preopen_live_partition_is_still_flagged(tmp_path):
    """整分区都是盘前实时数据时仍须修复, 不能因零成交而放过。"""
    part = tmp_path / "kline_daily" / f"date={FRIDAY.isoformat()}"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"],
        "date": [FRIDAY, FRIDAY],
        "open": [10.0, 20.0],
        "high": [10.0, 20.0],
        "low": [10.0, 20.0],
        "close": [10.0, 20.0],
        "volume": [0.0, 0.0],
        "amount": [0.0, 0.0],
        "quote_ts": [_ts_ms(FRIDAY, time(9, 15))] * 2,
    }).write_parquet(part / "part.parquet")
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))

    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.table, i.kind) for i in issues] == [
        (FRIDAY, "kline_daily", "snapshot"),
    ]


def test_today_partition_is_never_flagged(tmp_path):
    # 今天的盘中 quote_ts 属正常实时更新
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, None)
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(9, 45)))
    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_realtime_daily_builder_drops_halted_rows_before_zero_fill():
    from app.services.quote_service import QuoteService

    result = QuoteService._build_daily([
        {
            "symbol": "600001.SH", "last_price": 10.0,
            "open": 9.9, "high": 10.1, "low": 9.8,
            "volume": 1000.0, "amount": 10000.0,
            "timestamp": _ts_ms(cn_today(), time(10, 0)),
        },
        {
            "symbol": "600002.SH", "last_price": 20.0,
            "open": 0.0, "high": 0.0, "low": 0.0,
            "volume": 0.0, "amount": 0.0,
            "timestamp": _ts_ms(cn_today(), time(9, 15)),
        },
    ])

    assert result["symbol"].to_list() == ["600001.SH"]


def test_realtime_daily_builder_drops_stale_snapshot_rows():
    """回归: 停牌股快照停留旧日, 不得复制成当日假蜡烛 (301266.SZ 2026-09-04)。

    实时源对停牌标的返回停牌前最后一份快照 — OHLCV 全为旧日真实值, 仅
    timestamp 停在旧日。这种记录不属于当日, 必须在落盘前按 quote_ts 过滤。
    """
    from app.services.quote_service import QuoteService

    halted_since = cn_today() - timedelta(days=7)
    result = QuoteService._build_daily([
        {
            "symbol": "600001.SH", "last_price": 10.0,
            "open": 9.9, "high": 10.1, "low": 9.8,
            "volume": 1000.0, "amount": 10000.0,
            "timestamp": _ts_ms(cn_today(), time(15, 0)),
        },
        {
            "symbol": "301266.SZ", "last_price": 24.97,
            "open": 23.10, "high": 24.99, "low": 23.01,
            "volume": 65038.0, "amount": 157796300.0,
            "timestamp": _ts_ms(halted_since, time(15, 30)),
        },
    ])

    assert result["symbol"].to_list() == ["600001.SH"]


def test_realtime_daily_builder_keeps_rows_without_timestamp():
    """无时间戳的源无法判断快照新旧, 维持原行为保留 (不因缺列误删)。"""
    from app.services.quote_service import QuoteService

    result = QuoteService._build_daily([
        {
            "symbol": "600003.SH", "last_price": 8.0,
            "open": 7.9, "high": 8.1, "low": 7.8,
            "volume": 500.0, "amount": 4000.0,
        },
    ])

    assert result["symbol"].to_list() == ["600003.SH"]


def test_halt_filter_drops_legacy_zero_volume_row_after_ohlc_fill():
    from app.indicators.pipeline import filter_halt_days

    result = filter_halt_days(pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"],
        "open": [10.0, 20.0],
        "high": [10.2, 20.0],
        "volume": [1000.0, 0.0],
        "amount": [10100.0, 0.0],
    }))

    assert result["symbol"].to_list() == ["600001.SH"]


def test_missing_tail_day_flagged(tmp_path):
    # 周四有数据, 周五(工作日)整天停机缺失, 今天周一启动
    _write_daily_partition(tmp_path, "kline_daily", THURSDAY, None)
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.table, i.kind) for i in issues] == [
        (FRIDAY, "kline_daily", "missing"),
    ]


def test_snapshot_and_missing_both_reported(tmp_path):
    # 周四盘中快照 + 周五缺失
    _write_daily_partition(tmp_path, "kline_daily", THURSDAY, _ts_ms(THURSDAY, time(13, 30)))
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    kinds = {(i.day, i.kind) for i in issues}
    assert (THURSDAY, "snapshot") in kinds
    assert (FRIDAY, "missing") in kinds
    assert earliest_issue_day(issues) == THURSDAY


def test_no_recent_activity_not_flagged(tmp_path):
    # 最新分区早于扫描窗口 → 整族跳过 (首次启动/长期停用不自动修复)
    old = TODAY - timedelta(days=30)
    _write_daily_partition(tmp_path, "kline_daily", old, None)
    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_interior_history_hole_not_flagged(tmp_path):
    # 历史内部空洞是 laggards 另一类问题, 只报"晚于本地最新分区"的尾部缺口
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, None)
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert issues == []


def test_etf_family_independent(tmp_path):
    # ETF 族近期无活动 → 不判定, 即便股票族有问题
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, _ts_ms(FRIDAY, time(11, 58)))
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert all(i.table == "kline_daily" for i in issues)
    assert earliest_issue_day(issues, ("kline_etf_daily",)) is None


def test_auto_repair_window():
    assert within_auto_repair_window(TODAY - timedelta(days=AUTO_REPAIR_MAX_LAG_DAYS), today=TODAY) is True
    assert within_auto_repair_window(TODAY - timedelta(days=AUTO_REPAIR_MAX_LAG_DAYS + 1), today=TODAY) is False
    assert within_auto_repair_window(None, today=TODAY) is False


# ── enriched 分区删除 ───────────────────────────────────────────────


def test_prune_enriched_partitions_removes_only_range(tmp_path):
    base = tmp_path / "kline_daily_enriched"
    for day in (THURSDAY, FRIDAY, TODAY):
        part = base / f"date={day.isoformat()}"
        part.mkdir(parents=True)
        (part / "part.parquet").write_bytes(b"x")
    removed = prune_enriched_partitions(tmp_path, FRIDAY)
    assert removed == 2
    assert (base / f"date={THURSDAY.isoformat()}").exists()
    assert not (base / f"date={FRIDAY.isoformat()}").exists()
    assert not (base / f"date={TODAY.isoformat()}").exists()


# ── 管道起点决策 (分支3降级后的起点) ────────────────────────────────


def _resolve_daily_sync_start(latest_daily, stale_day):
    # 与 daily_pipeline.run_now 分支4的起点表达式一致 (min(非空值))
    return min(d for d in (latest_daily, stale_day) if d is not None)


def test_branch4_start_takes_earliest_bad_day():
    # today_exists 场景: latest=今天, 坏日=上周五 → 起点必须是上周五
    assert _resolve_daily_sync_start(TODAY, FRIDAY) == FRIDAY


def test_branch4_start_without_stale_day_uses_latest():
    assert _resolve_daily_sync_start(FRIDAY, None) == FRIDAY
    assert _resolve_daily_sync_start(TODAY, None) == TODAY


def test_timezone_conversion_is_cn():
    # quote_ts 是毫秒 Unix 时间戳, 必须按 UTC+8 折算 — 15:00 边界用例
    ts = int(datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc).timestamp() * 1000)  # 北京 15:00
    assert _is_snapshot(FRIDAY, ts) is False
    ts_morning = int(datetime(2026, 8, 21, 3, 58, tzinfo=timezone.utc).timestamp() * 1000)  # 北京 11:58
    assert _is_snapshot(FRIDAY, ts_morning) is True


def test_issue_from_other_day_timestamp_not_flagged(tmp_path):
    # 防御: quote_ts 日期与分区日期不符(跨天写入的脏数据)不判快照
    part = tmp_path / "kline_daily" / f"date={FRIDAY.isoformat()}"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["a"], "date": [FRIDAY], "quote_ts": [_ts_ms(THURSDAY, time(11, 0))],
    }).write_parquet(part / "part.parquet")
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    # 周五分区带周四时间戳 → 不判快照; 周四分区缺失且晚于最新(周五) → 不报
    assert issues == []


def test_describe_and_issue_dataclass():
    issues = [IntegrityIssue(day=FRIDAY, table="kline_daily", kind="snapshot")]
    from app.services.data_integrity import describe_issues

    assert "2026-08-21" in describe_issues(issues)
    assert "盘中快照" in describe_issues(issues)


# ── 开实时行情门禁 (钩子2) ──────────────────────────────────────────


def _gate_state(tmp_path, quote_service, repo, *, has_data=True):
    from types import SimpleNamespace

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            quote_service=quote_service,
            depth_service=None,
            repo=SimpleNamespace(
                store=SimpleNamespace(data_dir=tmp_path),
                # 首用门禁判据: 日K/enriched 最近日期 (None = 本地无数据)
                latest_daily_date=lambda: date(2026, 8, 28) if has_data else None,
                latest_enriched_date=lambda: None,
            ),
            capabilities=None,
        ))
    )


class _QuoteServiceStub:
    def __init__(self, mode="market"):
        self._mode = mode
        self.enabled = False

    @staticmethod
    def is_realtime_allowed():
        return True

    @staticmethod
    def is_paused():
        return False

    @staticmethod
    def realtime_mode():
        return "market"

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False


def test_realtime_gate_blocks_on_snapshot_and_launches_repair(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.api import settings as settings_api
    from app.services import data_integrity

    real_today = datetime.now(CN_TZ).date()
    snapshot_day = _recent_trading_day_before(real_today)
    _write_daily_partition(
        tmp_path, "kline_daily", snapshot_day,
        _ts_ms(snapshot_day, time(11, 58)),
    )
    _write_daily_partition(
        tmp_path, "kline_daily", real_today,
        _ts_ms(real_today, time(10, 0)),
    )

    launched = []
    monkeypatch.setattr(
        data_integrity, "launch_integrity_repair",
        lambda state, day, reason: (launched.append((day, reason)) or ("job-x", True)),
    )
    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.save", lambda payload: saved.update(payload),
    )

    qs = _QuoteServiceStub()
    request = _gate_state(tmp_path, qs, repo=None)
    req = settings_api.RealtimeQuotesPrefs(realtime_quotes_enabled=True)

    with pytest.raises(HTTPException) as exc_info:
        settings_api.update_realtime_quotes(req, request)

    assert exc_info.value.status_code == 409
    assert "盘中快照" in exc_info.value.detail
    assert "job-x" in exc_info.value.detail
    # 修复任务以最早坏日为起点, 且实时行情未被开启
    assert launched == [(snapshot_day, "realtime_gate")]
    assert saved == {}


def test_realtime_gate_blocks_when_no_local_data(tmp_path, monkeypatch):
    """首用门禁: 日K/enriched 均无数据时禁止开启实时行情 (409 + 同步指引),
    且不落偏好 (开关不生效)。"""
    from fastapi import HTTPException

    from app.api import settings as settings_api

    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.save", lambda payload: saved.update(payload),
    )

    qs = _QuoteServiceStub()
    request = _gate_state(tmp_path, qs, repo=None, has_data=False)
    req = settings_api.RealtimeQuotesPrefs(realtime_quotes_enabled=True)

    with pytest.raises(HTTPException) as exc_info:
        settings_api.update_realtime_quotes(req, request)

    assert exc_info.value.status_code == 409
    assert "同步" in exc_info.value.detail
    assert saved == {}                       # 未开启


def test_realtime_gate_allows_clean_data(tmp_path, monkeypatch):
    from app.api import settings as settings_api

    real_today = datetime.now(CN_TZ).date()
    previous_day = _recent_trading_day_before(real_today)
    _write_daily_partition(tmp_path, "kline_daily", previous_day, None)
    _write_daily_partition(
        tmp_path, "kline_daily", real_today,
        _ts_ms(real_today, time(10, 0)),
    )

    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.save", lambda payload: saved.update(payload),
    )
    qs = _QuoteServiceStub()
    request = _gate_state(tmp_path, qs, repo=None)
    req = settings_api.RealtimeQuotesPrefs(realtime_quotes_enabled=True)

    result = settings_api.update_realtime_quotes(req, request)
    assert result["realtime_quotes_enabled"] is True
    assert qs.enabled is True
    assert saved == {"realtime_quotes_enabled": True}


def test_realtime_gate_ignores_old_issues_beyond_window(tmp_path, monkeypatch):
    from app.api import settings as settings_api

    real_today = datetime.now(CN_TZ).date()
    old_day = real_today - timedelta(days=AUTO_REPAIR_MAX_LAG_DAYS + 1)
    while old_day.weekday() >= 5:
        old_day -= timedelta(days=1)
    _write_daily_partition(tmp_path, "kline_daily", old_day, _ts_ms(old_day, time(11, 58)))
    _write_daily_partition(
        tmp_path, "kline_daily", real_today,
        _ts_ms(real_today, time(10, 0)),
    )

    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.save", lambda payload: saved.update(payload),
    )
    qs = _QuoteServiceStub()
    request = _gate_state(tmp_path, qs, repo=None)
    req = settings_api.RealtimeQuotesPrefs(realtime_quotes_enabled=True)

    result = settings_api.update_realtime_quotes(req, request)
    assert result["realtime_quotes_enabled"] is True


def test_boot_check_launches_repair_within_window(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.services import data_integrity

    # boot_integrity_check 用真实"今天" — 往回找最近工作日造盘中快照分区
    launched = []
    monkeypatch.setattr(
        data_integrity, "launch_integrity_repair",
        lambda state, day, reason: (launched.append(day) or ("job-x", True)),
    )

    real_today = datetime.now(CN_TZ).date()
    probe = _recent_trading_day_before(real_today)
    data_dir = tmp_path / "boot"
    _write_daily_partition(data_dir, "kline_daily", probe, _ts_ms(probe, time(11, 58)))
    _write_daily_partition(data_dir, "kline_daily", real_today, _ts_ms(real_today, time(10, 0)))

    state = SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)),
    )
    data_integrity.boot_integrity_check(state)
    assert launched == [probe]


# ── 管道自愈端到端 (钩子3, 离线集成) ────────────────────────────────


def _write_full_partition(root, table: str, day: date, quote_ts: int | None) -> None:
    part = root / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"],
        "date": [day, day],
        "open": [10.0, 20.0], "high": [10.1, 20.1],
        "low": [9.9, 19.9], "close": [10.0, 20.0],
        "volume": [100.0, 200.0], "amount": [1000.0, 4000.0],
        "quote_ts": [quote_ts, quote_ts],
    }).write_parquet(part / "part.parquet")


def test_pipeline_self_heals_snapshot_day(tmp_path, monkeypatch):
    """用户 bug 场景复刻: 昨天盘中快照 + 今天实时分区 → 管道应放弃"只刷今天",
    降级为从坏日起的范围拉取, 并把坏 enriched 分区删后重算。"""
    from app.config import settings as app_settings
    from app.jobs import daily_pipeline
    from app.services import instrument_sync, kline_sync
    from app.tickflow.repository import DataStore, KlineRepository

    today = datetime.now(CN_TZ).date()
    yesterday = _recent_trading_day_before(today)

    _write_full_partition(tmp_path, "kline_daily", yesterday, _ts_ms(yesterday, time(11, 58)))
    _write_full_partition(tmp_path, "kline_daily", today, _ts_ms(today, time(10, 0)))
    _write_full_partition(tmp_path, "kline_daily_enriched", yesterday, _ts_ms(yesterday, time(11, 58)))
    _write_full_partition(tmp_path, "kline_daily_enriched", today, _ts_ms(today, time(10, 0)))

    # 网络函数离线化: 维表同步 + 日K batch 拉取(记录参数)
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    batch_calls: list[dict] = []

    def _fake_batch(universe, repo, capset, start_date=None, end_date=None, on_chunk_done=None):
        batch_calls.append({
            "start": start_date.date() if hasattr(start_date, "date") else start_date,
            "end": end_date.date() if hasattr(end_date, "date") else end_date,
        })
        return 0

    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", _fake_batch)
    # run_pipeline() 不传 data_dir 时读 settings.data_dir — 同步指到 tmp
    monkeypatch.setattr(app_settings, "data_dir", tmp_path)

    repo = KlineRepository(DataStore(tmp_path))
    capset = SimpleNamespace(has=lambda key: key == "QUOTE_POOL")

    result = daily_pipeline.run_now(repo, capset)  # type: ignore[arg-type]

    # 分支3(实时覆写只刷今天)被降级 → 范围拉取起点=坏日
    assert batch_calls and batch_calls[0]["start"] == yesterday
    assert result["integrity_repair_from"] == yesterday.isoformat()
    assert result["integrity_issues"] >= 1
    # 坏 enriched 分区被删后当"新日期"重算写回 (无 prune 时 Step 2 走 skip 不写)
    enriched_left = sorted(
        p.name for p in (tmp_path / "kline_daily_enriched").glob("date=*")
    )
    assert enriched_left == [f"date={yesterday.isoformat()}", f"date={today.isoformat()}"]
    assert result["enriched_days"] > 0


def test_quotes_flush_partition_keeps_quote_ts_for_integrity_scan(tmp_path, monkeypatch):
    """实时行情覆写当日分区必须写 quote_ts, 否则停机后自检漏判盘中快照。

    盘中手动触发盘后管道时, "今天已有数据 → 实时行情覆写"分支会用
    tf.quotes.get_by_universes 整分区覆写。若覆写不带 quote_ts, 停机后次日
    启动自检把这份半日数据当成 batch 权威历史, 停机时刻的 close/volume 永久
    留存并污染 lookback 指标 —— 正是本模块要拦的场景。
    """
    from app.services import kline_sync
    from app.tickflow import client as tf_client
    from app.tickflow.repository import DataStore, KlineRepository

    snapshot_ms = _ts_ms(FRIDAY, time(11, 58))

    class _FakeQuotes:
        @staticmethod
        def get_by_universes(universes):
            return [{
                "symbol": "600001.SH",
                "open": 10.0, "high": 10.2, "low": 9.8, "last_price": 10.1,
                "volume": 1000.0, "amount": 10100.0,
                "timestamp": snapshot_ms,
            }]

    monkeypatch.setattr(tf_client, "get_client", lambda: SimpleNamespace(quotes=_FakeQuotes))
    monkeypatch.setattr(kline_sync, "cn_today", lambda: FRIDAY)

    repo = KlineRepository(DataStore(tmp_path))
    assert kline_sync.sync_daily_by_quotes(repo) == 1

    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.table, i.kind) for i in issues] == [(FRIDAY, "kline_daily", "snapshot")]

    part_dir = tmp_path / "kline_daily" / f"date={FRIDAY.isoformat()}"
    assert _quote_ts_max_ms(part_dir) == snapshot_ms


# ── 交易日历: 候选日真实化 (2026 中秋 09-25 休市误报 missing → gate 409) ──
#
# issue #442: _candidate_days 用「周一~周五」近似交易日, 中秋休市日 09-25(周五)
# 被当缺失日, realtime_gate 循环 409 (修复任务对休市日永远拉不到数据)。

_CAL_FAKE_2026_09 = {
    date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24),
    date(2026, 9, 28),
}  # 2026-09-25(五)~09-27(日) 中秋休市, 09-28(一) 开市


@pytest.fixture(autouse=True)
def _no_trading_calendar_by_default():
    """默认把模块级日历缓存置为「过期」: 既有用例维持周几近似语义 (CI 无
    fuyao → 探测即回退, 零外部依赖); 日历相关用例自行写入 fake 日历。
    (fetched_at=0.0 表示 TTL 已过, 不用 time.monotonic — 本文件顶部
    `from datetime import time` 会遮蔽标准库 time。)"""
    saved = di._CAL
    di._CAL = (0.0, None)
    yield
    di._CAL = saved


def _patch_fuyao_calendar(monkeypatch, days):
    fake = SimpleNamespace(
        is_custom_provider=lambda name: True,
        get_provider=lambda name: SimpleNamespace(trading_days=lambda: set(days)),
    )
    monkeypatch.setattr(app.data_providers, "custom", fake)
    di._CAL = (0.0, None)  # 清缓存, 强制本用例走自己的日历


def _recent_trading_day_before(day: date) -> date:
    """严格早于 day 的最近交易日: 日历可用按日历, 否则周几近似。

    取代旧的 `while d.weekday() >= 5` 探针 —— 节假日(如 2026 中秋 09-25)
    后该探针会命中休市日, 而休市日在日历语义下不再是候选日。
    """
    cal = _trading_calendar()
    d = day - timedelta(days=1)
    while True:
        if cal is not None:
            if d in cal:
                return d
        elif d.weekday() < 5:
            return d
        d -= timedelta(days=1)


def test_candidate_days_excludes_market_holiday(monkeypatch):
    """中秋休市日不得进候选日; 真实交易日保留; 今天被严格排除。"""
    _patch_fuyao_calendar(monkeypatch, _CAL_FAKE_2026_09)
    got = _candidate_days(date(2026, 9, 28), 7)
    assert date(2026, 9, 25) not in got
    assert date(2026, 9, 24) in got
    assert date(2026, 9, 28) not in got


def test_candidate_days_falls_back_to_weekday_without_calendar(monkeypatch):
    """从未取到日历 (fuyao 未配置/失败) → 回退周几近似 (历史行为)。"""
    fake = SimpleNamespace(is_custom_provider=lambda name: False)
    monkeypatch.setattr(app.data_providers, "custom", fake)
    got = _candidate_days(date(2026, 9, 28), 7)
    assert got == [
        date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23),
        date(2026, 9, 24), date(2026, 9, 25),
    ]


def test_calendar_fetch_failure_serves_last_known(monkeypatch):
    """fuyao 抖动时沿用上次成功结果 (stale-while-error), 不退回周几近似。"""
    _patch_fuyao_calendar(monkeypatch, _CAL_FAKE_2026_09)
    assert _trading_calendar() == _CAL_FAKE_2026_09

    def _boom():
        raise RuntimeError("fuyao down")

    fake = SimpleNamespace(
        is_custom_provider=lambda name: True,
        get_provider=lambda name: SimpleNamespace(trading_days=_boom),
    )
    monkeypatch.setattr(app.data_providers, "custom", fake)
    di._CAL = (0.0, _CAL_FAKE_2026_09)  # fetched_at=0 → TTL 过期 → 重新探测失败
    assert _trading_calendar() == _CAL_FAKE_2026_09


def test_scan_no_false_missing_when_etf_lags_over_holiday(tmp_path, monkeypatch):
    """端到端: ETF 表停在休市前最后一个交易日 + 日历可用 → 扫描不误报 missing。"""
    _patch_fuyao_calendar(monkeypatch, _CAL_FAKE_2026_09)
    for day in (date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)):
        _write_daily_partition(tmp_path, "kline_etf_daily", day, None)
    assert scan_recent_integrity(tmp_path, today=date(2026, 9, 28)) == []
