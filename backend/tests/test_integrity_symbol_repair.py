"""#514 — symbol 级判坏 / 定向修复 / 后置校验 / 自动修复退避。"""
from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from unittest.mock import patch

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import data_integrity

TODAY = datetime.now(CN_TZ).date()


def _ts_ms(day: date, t: time) -> int:
    return int(datetime.combine(day, t, tzinfo=CN_TZ).timestamp() * 1000)


def _recent_trading_day_before(today: date, n: int = 1) -> date:
    d = today
    for _ in range(n * 3):
        d = date.fromordinal(d.toordinal() - 1)
        if d.weekday() < 5:
            n -= 1
            if n == 0:
                return d
    raise AssertionError


def _write_mixed_partition(root, table: str, day: date, rows: list[tuple[str, int | None, float]]) -> None:
    """rows: (symbol, quote_ts, volume)。quote_ts=None 是 batch 权威行。"""
    part = root / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "date": [day] * len(rows),
        "open": [10.0] * len(rows),
        "high": [10.1] * len(rows),
        "low": [9.9] * len(rows),
        "close": [10.0] * len(rows),
        "volume": [r[2] for r in rows],
        "amount": [r[2] * 10 for r in rows],
        "quote_ts": [r[1] for r in rows],
    }).write_parquet(part / "part.parquet")


# ── 判据: symbol 级缺陷定位 ──────────────────────────────────────────


def test_laggard_residue_amid_authoritative_rows_is_clean(tmp_path):
    """#514 反面场景: 有成交实时残留的 symbol 同时已有 batch 权威行 → 分区干净。"""
    day = _recent_trading_day_before(TODAY)
    _write_mixed_partition(tmp_path, "kline_daily", day, [
        ("600001.SH", None, 100.0),
        ("600002.SH", None, 200.0),
        # 600001 的 14:59 实时残留 (有成交) — 但它已有权威行, 无害
        ("600001.SH", _ts_ms(day, time(14, 59, 59)), 50.0),
    ])
    issues = data_integrity.scan_recent_integrity(tmp_path, today=TODAY)
    assert issues == []


def test_defect_symbols_reported_without_authoritative_rows(tmp_path):
    """#514 正面场景: 残留 symbol 无权威行 → 分区坏, 且 symbols 恰为这几只。"""
    day = _recent_trading_day_before(TODAY)
    _write_mixed_partition(tmp_path, "kline_daily", day, [
        ("600001.SH", None, 100.0),
        ("600002.SH", None, 200.0),
        ("002005.SZ", _ts_ms(day, time(14, 59, 59)), 50.0),  # 无权威行 → 缺陷
        ("002009.SZ", _ts_ms(day, time(14, 59, 58)), 60.0),  # 无权威行 → 缺陷
    ])
    issues = data_integrity.scan_recent_integrity(tmp_path, today=TODAY)
    snapshots = [i for i in issues if i.kind == "snapshot" and i.table == "kline_daily"]
    assert len(snapshots) == 1
    assert snapshots[0].symbols == ("002005.SZ", "002009.SZ")


def test_stock_snapshot_symbols_helper():
    issues = [
        data_integrity.IntegrityIssue(date(2026, 10, 26), "kline_daily", "snapshot",
                                      symbols=("A", "B")),
        data_integrity.IntegrityIssue(date(2026, 10, 26), "kline_etf_daily", "snapshot",
                                      symbols=("510300.SH",)),  # ETF 族不并入股票修复
        data_integrity.IntegrityIssue(date(2026, 10, 25), "kline_daily", "missing"),
    ]
    assert data_integrity._stock_snapshot_symbols(issues) is None  # 整日缺失须转全市场修补
    assert data_integrity._stock_snapshot_symbols(issues[:2]) == ["A", "B"]
    assert data_integrity._stock_snapshot_symbols(issues[1:2]) is None


# ── 定向修复 + 后置校验 + 单票失败上浮 ────────────────────────────────


def _offline_env(tmp_path, monkeypatch):
    from app.config import settings as app_settings
    from app.jobs import daily_pipeline
    from app.services import instrument_sync, kline_sync
    from app.tickflow.repository import DataStore, KlineRepository

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    calls: list[dict] = []
    knobs = {"fail": [], "fix": [], "fix_day": TODAY, "keep": []}

    def _fake_batch(symbols, repo, capset, start_date=None, end_date=None,
                    on_chunk_done=None, failed_out=None):
        calls.append({"symbols": list(symbols)})
        if failed_out is not None and knobs["fail"]:
            failed_out.extend(knobs["fail"])
        if knobs["fix"]:
            # 模拟数据源修好坏标的: 重写分区, 坏 symbol 变权威行, 其余保留
            _write_mixed_partition(
                tmp_path, "kline_daily", knobs["fix_day"],
                [(s, None, 100.0) for s in knobs["fix"]] + knobs["keep"],
            )
        return 0

    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", _fake_batch)

    repo = KlineRepository(DataStore(tmp_path))
    capset = SimpleNamespace(has=lambda key: key == "QUOTE_POOL")
    state = SimpleNamespace(calls=calls, knobs=knobs)
    return daily_pipeline, repo, capset, state


def test_targeted_repair_passes_defect_symbols_and_selfheals(tmp_path, monkeypatch):
    day = _recent_trading_day_before(TODAY)
    _write_mixed_partition(tmp_path, "kline_daily", day, [
        ("600001.SH", None, 100.0),
        ("600002.SH", None, 200.0),
        ("002005.SZ", _ts_ms(day, time(14, 59, 59)), 50.0),
    ])
    pipeline, repo, capset, state = _offline_env(tmp_path, monkeypatch)
    state.knobs["fix"] = ["002005.SZ"]
    state.knobs["fix_day"] = day
    state.knobs["keep"] = [("600001.SH", None, 100.0), ("600002.SH", None, 200.0)]

    result = pipeline.run_now(
        repo, capset,  # type: ignore[arg-type]
        override_start_date=day, override_symbols=["002005.SZ"],
    )
    assert state.calls, "日K batch 必须被调用"
    assert state.calls[0]["symbols"] == ["002005.SZ"]  # 只拉坏标的, 不是全 universe
    assert result["integrity_repair_from"] == day.isoformat()
    # 后置校验通过 (fake batch 已把坏行写成权威行)
    assert _no_issues(tmp_path)


def _no_issues(tmp_path) -> bool:
    return data_integrity.scan_recent_integrity(tmp_path, today=TODAY) == []


def test_post_repair_check_fails_job_when_issues_remain(tmp_path, monkeypatch):
    day = _recent_trading_day_before(TODAY)
    _write_mixed_partition(tmp_path, "kline_daily", day, [
        ("600001.SH", None, 100.0),
        ("002005.SZ", _ts_ms(day, time(14, 59, 59)), 50.0),
    ])
    pipeline, repo, capset, state = _offline_env(tmp_path, monkeypatch)
    state.knobs["fail"] = ["002005.SZ"]  # 数据源拉取失败 + 分区未修好

    from app.jobs.daily_pipeline import PipelineStageError

    with pytest.raises(PipelineStageError) as ei:
        pipeline.run_now(
            repo, capset,  # type: ignore[arg-type]
            override_start_date=day, override_symbols=["002005.SZ"],
        )
    joined = "\n".join(str(e) for e in ei.value.errors)
    assert "完整性仍不通过" in joined
    assert "拉取失败" in joined
    assert "002005.SZ" in joined


@pytest.mark.parametrize("manual,source_empty", [(True, False), (False, False), (True, True)])
def test_etf_repair_fills_missing_days_before_latest_enriched(
    tmp_path, monkeypatch, manual, source_empty,
):
    """ETF 指标缓存较新时, 指定历史修复起点仍须真正传给 ETF 日线同步。"""
    from app.services import index_sync, preferences
    from app.tickflow.capabilities import Cap

    today = date(2026, 10, 30)
    missing = [date(2026, 10, 28), date(2026, 10, 29)]
    _write_mixed_partition(tmp_path, "kline_etf_daily", date(2026, 10, 27), [
        ("510300.SH", None, 100.0),
    ])
    (tmp_path / "kline_etf_enriched" / "date=2026-10-30").mkdir(parents=True)
    pipeline, repo, _, _ = _offline_env(tmp_path, monkeypatch)
    monkeypatch.setattr(pipeline, "cn_today", lambda: today)
    monkeypatch.setattr(pipeline, "_resolve_universe", lambda *args: ["000001.SZ"])
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kwargs: 0)
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: None)
    monkeypatch.setattr(preferences, "load", lambda: {
        "pipeline_pull_a_share": False,
        "pipeline_pull_index": False,
        "pipeline_pull_etf": True,
        "daily_data_provider": "fuyao",
        "adj_factor_provider": "tickflow",
        "minute_sync_enabled": False,
        "pipeline_regime_enabled": False,
    })
    monkeypatch.setattr(index_sync, "sync_etf_instruments", lambda repo: 0)
    monkeypatch.setattr(repo, "get_etf_instruments", lambda: pl.DataFrame({"symbol": ["510300.SH"]}))
    calls = []

    def sync_etf(repo, capset, start_date, end_date, **kwargs):
        calls.append(start_date.date())
        if source_empty:
            return 0
        written = 0
        for day in missing:
            if start_date.date() <= day <= end_date.date():
                _write_mixed_partition(tmp_path, "kline_etf_daily", day, [
                    ("510300.SH", None, 100.0),
                ])
                written += 1
        return written

    monkeypatch.setattr(index_sync, "sync_and_persist_etf_daily", sync_etf)
    capset = SimpleNamespace(has=lambda key: key == Cap.KLINE_DAILY_BATCH)

    if source_empty:
        with pytest.raises(pipeline.PipelineStageError) as error:
            pipeline.run_now(repo, capset, override_start_date=missing[0])
        assert "ETF" in str(error.value)
        assert "2026-10-28" in str(error.value)
        assert "数据源拉取失败" not in str(error.value)
    else:
        result = pipeline.run_now(
            repo, capset, override_start_date=missing[0] if manual else None,
        )
        assert result["stage_errors"] == []
        assert data_integrity.scan_recent_integrity(tmp_path, today=today) == []
    assert calls == [missing[0]]


# ── 自动修复退避 ─────────────────────────────────────────────────────


def _fake_app_state(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        capabilities=SimpleNamespace(has=lambda key: True),  # 能力恒过, 只测退避分支
        quote_service=None,
    )


def test_backoff_blocks_then_expires(tmp_path):
    day = date(2026, 10, 26)
    assert data_integrity.repair_backoff_remaining_s(tmp_path, day) == 0.0
    data_integrity._record_repair_attempt(tmp_path, day)
    remaining = data_integrity.repair_backoff_remaining_s(tmp_path, day)
    assert 0 < remaining <= data_integrity.INTEGRITY_REPAIR_BACKOFF_S
    state = _fake_app_state(tmp_path)
    assert data_integrity.launch_integrity_repair(state, day, "boot_integrity_check") == (None, False)
    # 冷却期满 → 放行 (会真实建 job, 用完清理)
    with patch.object(data_integrity.time, "time", lambda: 1e18):
        assert data_integrity.repair_backoff_remaining_s(tmp_path, day) == 0.0
        job_id, is_new = data_integrity.launch_integrity_repair(state, day, "boot_integrity_check")
        assert is_new is True
        from app.services.pipeline_jobs import job_store

        job_store.terminate(job_id, "test cleanup")


def test_gate_message_mentions_backoff(tmp_path, monkeypatch):
    """退避期内 gate 409 文案指向手动修正, 而不是再建任务。"""
    day = _recent_trading_day_before(TODAY)
    _write_mixed_partition(tmp_path, "kline_daily", day, [
        ("600001.SH", None, 100.0),
        ("002005.SZ", _ts_ms(day, time(14, 59, 59)), 50.0),
    ])
    data_integrity._record_repair_attempt(tmp_path, day)

    created: list[dict] = []
    with patch.object(
        data_integrity, "launch_integrity_repair",
        side_effect=lambda state, start, reason, symbols=None: (
            created.append({"start": start, "symbols": symbols}) or (None, False)
        ),
    ):
        remaining = data_integrity.repair_backoff_remaining_s(tmp_path, day)
        assert remaining > 0
        # gate 的行为: launch 返回 (None, False) 且退避中 → 409 手动修正指引
        # (此处直接验证退避查询; 端点级 409 由 settings 端到端覆盖)
    assert not created
