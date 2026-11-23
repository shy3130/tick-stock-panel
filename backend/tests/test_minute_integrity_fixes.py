"""分钟数据完整性修复的回归锁定。

四项: ① 全量分钟服务落盘持仓库写锁 + 原子写临时名唯一化; ② 服务暴露 raw 基准
状态; ③ minute-batch 门控在全量分钟服务健康时放行本地读; ④ 补拉限速与盘后
同步统一走 resolve_limit (×0.8 安全系数, 同 rpm 桶)。
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.services import kline_sync
from app.services.minute_refresh import MinuteRefreshService
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

_BJ = date(2026, 10, 23)
_SYM = "600519.SH"


# ─────────────────────────── ① 服务落盘持写锁 ───────────────────────────


class _RecordingLock:
    """记录获取次数的锁包装; spy 在写分区时检查它是否被持有。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return None

    def locked(self) -> bool:
        return self._lock.locked()


def _round_df() -> pl.DataFrame:
    start = datetime(_BJ.year, _BJ.month, _BJ.day, 10, 0)
    return pl.DataFrame({
        "symbol": [_SYM] * 3,
        "datetime": [start + timedelta(minutes=i) for i in range(3)],
        "close": [10.0, 10.1, 10.2],
    })


def test_service_write_holds_repo_write_lock(tmp_path, monkeypatch):
    """服务增量轮落盘必须持 repo._write_lock (与端点补拉/盘后同步同一纪律)。"""
    lock = _RecordingLock()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), _write_lock=lock)
    svc = MinuteRefreshService(repo)
    monkeypatch.setattr(svc, "_resolve_custom", lambda: (None, "tickflow"))
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")

    df = _round_df()
    monkeypatch.setattr(
        kline_sync, "fetch_intraday_universe_increment", lambda: (df, 1)
    )

    seen: dict[str, object] = {}

    def spy_write(frame, minute_dir):
        seen["locked"] = lock.locked()
        return frame.height

    monkeypatch.setattr(kline_sync, "_write_minute_partition", spy_write)

    svc._run_round()

    assert seen.get("locked") is True, "落盘时必须持有仓库写锁"


def test_service_records_minute_basis_state(tmp_path, monkeypatch):
    """② 每轮记录本地库 raw 基准状态; 未迁移 (无 .raw_basis) 时只警告一次。"""
    lock = _RecordingLock()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), _write_lock=lock)
    svc = MinuteRefreshService(repo)
    monkeypatch.setattr(svc, "_resolve_custom", lambda: (None, "tickflow"))
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")
    monkeypatch.setattr(
        kline_sync, "fetch_intraday_universe_increment", lambda: (_round_df(), 1)
    )
    monkeypatch.setattr(kline_sync, "_write_minute_partition", lambda f, d: f.height)

    warnings: list[str] = []
    from app.services import minute_refresh as mr
    monkeypatch.setattr(mr.logger, "warning", lambda msg, *a: warnings.append(msg))

    svc._run_round()
    svc._run_round()

    assert svc._state.minute_basis_raw is False   # tmp_path 无 .raw_basis 标记
    assert len(warnings) == 1                     # 只警告一次
    assert "raw 基准" in warnings[0]


def test_atomic_write_tmp_names_unique(tmp_path, monkeypatch):
    """① 原子写的临时文件名必须每次唯一 (防两写者共用同一 tmp 交错损坏)。"""
    df = pl.DataFrame({"a": [1, 2, 3]})
    out = tmp_path / "part.parquet"

    captured: list[Path] = []
    monkeypatch.setattr(kline_sync, "replace_with_retry",
                        lambda tmp, dst: captured.append(tmp))

    kline_sync._atomic_write_parquet(df, out)
    kline_sync._atomic_write_parquet(df, out)

    assert len(captured) == 2
    assert captured[0] != captured[1]
    assert all(p.name.endswith(".tmp") for p in captured)   # 不匹配 *.parquet glob
    assert not list(tmp_path.glob("*.parquet"))             # 正常路径无残留


# ─────────────────────── ③ 门控: 服务健康放行本地读 ───────────────────────


def _bars(sym: str, n: int) -> pl.DataFrame:
    start = datetime(_BJ.year, _BJ.month, _BJ.day, 9, 31)
    return pl.DataFrame({
        "symbol": [sym] * n,
        "datetime": [start + timedelta(minutes=i) for i in range(n)],
        "open": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n, "close": [10.0] * n,
        "volume": [100.0] * n, "amount": [1000.0] * n,
    })


def _gate_env(monkeypatch, *, healthy: bool | None):
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ)
    monkeypatch.setattr(kline_api, "cn_now", lambda: datetime(_BJ.year, _BJ.month, _BJ.day, 15, 0))
    monkeypatch.setattr(kline_api, "in_continuous_session", lambda: True)

    repo = MagicMock()
    repo.get_etf_symbol_set.return_value = set()
    repo.get_minute_batch.return_value = _bars(_SYM, 240)   # 240 根 = 盘后完整, 不触发补拉

    capset = MagicMock()
    capset.has.return_value = False                          # 无 Pro+ BATCH 能力
    capset.limits.return_value = None

    svc = None
    if healthy is not None:
        svc = MagicMock()
        svc.is_healthy.return_value = healthy

    request = MagicMock()
    request.headers = {}
    request.app.state.repo = repo
    request.app.state.capabilities = capset
    # MagicMock 的属性访问恒返回 truthy mock, 服务缺席场景必须显式置 None
    request.app.state.minute_refresh = svc
    return kline_api, request


def test_gate_allows_local_read_when_service_healthy(monkeypatch):
    """③ 无 Pro+ 但全量分钟服务健康 (正写本地分区) → 本地读放行。"""
    kline_api, request = _gate_env(monkeypatch, healthy=True)
    resp = kline_api.get_minute_batch(request, {"symbols": [_SYM], "date": str(_BJ)})
    payload = json.loads(resp.body)
    assert len(payload["data"][_SYM]) == 240


def test_gate_rejects_when_service_absent_or_unhealthy(monkeypatch):
    """③ 服务缺席或不健康且无能力 → 维持 403 (回落现状, 拉取仍需 Pro+)。"""
    kline_api, request = _gate_env(monkeypatch, healthy=None)
    with pytest.raises(HTTPException) as exc:
        kline_api.get_minute_batch(request, {"symbols": [_SYM], "date": str(_BJ)})
    assert exc.value.status_code == 403

    kline_api, request = _gate_env(monkeypatch, healthy=False)
    with pytest.raises(HTTPException) as exc:
        kline_api.get_minute_batch(request, {"symbols": [_SYM], "date": str(_BJ)})
    assert exc.value.status_code == 403


# ─────────────── ④ 补拉限速与盘后同步统一 (×0.8, 同 rpm 桶) ───────────────


def test_pull_uses_resolve_limit_safety_rpm(monkeypatch):
    """④ 补拉 rpm 必须经 resolve_limit (60 → 48), 与盘后同步落同一共享桶。"""
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ)
    monkeypatch.setattr(kline_api, "cn_now", lambda: datetime(_BJ.year, _BJ.month, _BJ.day, 10, 30))
    monkeypatch.setattr(kline_api, "in_continuous_session", lambda: True)

    # 中间有洞 (间距 2min) → 全天补拉, 捕获传给 sync_minute_batch 的限速参数
    hole = pl.DataFrame({
        "symbol": [_SYM] * 2,
        "datetime": [datetime(_BJ.year, _BJ.month, _BJ.day, 9, 31),
                     datetime(_BJ.year, _BJ.month, _BJ.day, 9, 33)],
        "open": [10.0] * 2, "high": [10.1] * 2, "low": [9.9] * 2, "close": [10.0] * 2,
        "volume": [100.0] * 2, "amount": [1000.0] * 2,
    })
    repo = MagicMock()
    repo.get_etf_symbol_set.return_value = set()
    repo.get_minute_batch.return_value = hole
    repo._write_lock = threading.Lock()

    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200, rpm=60)})

    captured: dict[str, object] = {}

    def spy_sync(symbols, *, start_time, end_time, batch_size=None, rpm=None,
                 asset_type="stock", **kw):
        captured["batch"] = batch_size
        captured["rpm"] = rpm
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_minute_batch", spy_sync)

    request = MagicMock()
    request.headers = {}
    request.app.state.repo = repo
    request.app.state.capabilities = capset
    kline_api.get_minute_batch(request, {"symbols": [_SYM], "date": str(_BJ)})

    assert captured["batch"] == 200          # batch 原样透传
    assert captured["rpm"] == 48             # 60 × 0.8 安全系数, 与盘后同步同桶
