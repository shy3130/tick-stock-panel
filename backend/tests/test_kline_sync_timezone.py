"""时区契约测试 — 分时拉取窗口必须按北京时间解释, 与服务器本地时区无关。

fetch_minute_single 构造的 naive datetime 会被 _datetime_to_ms 的 .timestamp()
按服务器本地时区解释: UTC 容器 (Docker 默认) 上窗口偏移 8 小时, 补拉必为空。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl

from app.market_time import CN_TZ
from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def test_fetch_minute_single_window_is_beijing_wall_clock(monkeypatch):
    captured: dict[str, int] = {}

    def _fake_try_custom_minute(*args, **kwargs):
        return (None, True)  # 未配自定义源 → 走 TickFlow 分支

    class _FakeKlines:
        @staticmethod
        def batch(symbols, period, start_time, end_time, **kwargs):
            captured["start_ms"] = start_time
            captured["end_ms"] = end_time
            return []

    class _FakeClient:
        klines = _FakeKlines

    monkeypatch.setattr(kline_sync, "_try_custom_minute", _fake_try_custom_minute)
    monkeypatch.setattr(kline_sync, "get_client", lambda: _FakeClient())

    kline_sync.fetch_minute_single(
        "600000.SH",
        date(2026, 8, 14),
        capset=CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()}),
    )

    start = datetime.fromtimestamp(captured["start_ms"] / 1000, tz=CN_TZ)
    end = datetime.fromtimestamp(captured["end_ms"] / 1000, tz=CN_TZ)
    assert (start.date(), start.hour, start.minute) == (date(2026, 8, 14), 9, 25)
    assert (end.date(), end.hour, end.minute) == (date(2026, 8, 14), 15, 5)


def _capture_minute_window(monkeypatch, tmp_path, local_dt: datetime, **kwargs) -> dict:
    """桩掉 sync_and_persist_minute 的外部依赖, 只截获传给取数层的时间窗口。

    local_dt 模拟 kline_minute 里的极值 (落盘口径为北京墙钟 naive)。
    """
    captured: dict = {}

    def _fake_sync_minute_batch(symbols, **call_kwargs):
        captured.update(call_kwargs)
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)
    monkeypatch.setattr(kline_sync, "_cleanup_null_datetime_minute", lambda repo: None)
    monkeypatch.setattr(kline_sync, "_migrate_symbol_to_date_partition", lambda repo: None)
    monkeypatch.setattr(kline_sync, "resolve_limit", lambda *a, **kw: SimpleNamespace(batch=100, rpm=30))
    monkeypatch.setattr(kline_sync, "sync_minute_batch", _fake_sync_minute_batch)

    repo = MagicMock()
    repo.store.data_dir = tmp_path
    repo.execute_one = lambda sql: (local_dt,)
    kline_sync.sync_and_persist_minute(
        ["600000.SH"],
        repo,
        CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()}),
        **kwargs,
    )
    return captured


def test_sync_and_persist_minute_incremental_window_is_beijing_wall_clock(monkeypatch, tmp_path):
    """增量分钟同步: 起点取自本地分钟 K 的北京墙钟, 终点必须同口径。

    终点用服务器本地墙钟时, _datetime_to_ms 会把两端按同一本地时区换算 →
    UTC 容器上起点(北京 14:30)反而晚于终点, 一个请求都发不出去, 分钟 K 永远
    停在首次拉取的位置。
    """
    local_latest = datetime(2026, 8, 14, 14, 30)  # 本地最新分钟 K: 北京墙钟 naive

    captured = _capture_minute_window(monkeypatch, tmp_path, local_latest)
    start, end = captured["start_time"], captured["end_time"]

    assert start.utcoffset() == timedelta(hours=8)
    assert end.utcoffset() == timedelta(hours=8)
    assert start < end
    assert datetime.fromtimestamp(
        kline_sync._datetime_to_ms(start) / 1000, tz=CN_TZ
    ) == local_latest.replace(tzinfo=CN_TZ)


def test_sync_and_persist_minute_extend_backward_window_is_beijing_wall_clock(monkeypatch, tmp_path):
    """向前扩展: 终点取自本地最早分钟 K 的北京墙钟, 同样不能被服务器时区重解释。"""
    local_earliest = datetime(2026, 8, 14, 9, 30)

    captured = _capture_minute_window(
        monkeypatch, tmp_path, local_earliest, days=5, extend_backward=True,
    )
    start, end = captured["start_time"], captured["end_time"]

    assert start.utcoffset() == timedelta(hours=8)
    assert end.utcoffset() == timedelta(hours=8)
    assert (start.hour, start.minute) == (9, 25)
    assert end.date() - start.date() == timedelta(days=7)  # 5 交易日 x 7/5
    assert datetime.fromtimestamp(
        kline_sync._datetime_to_ms(end) / 1000, tz=CN_TZ
    ) == local_earliest.replace(tzinfo=CN_TZ)


def _capture_tickflow_minute_batch(monkeypatch) -> tuple[list[tuple[datetime, datetime]], list]:
    """桩掉 TickFlow 客户端, 截获 sync_minute_batch 发出的窗口 (换算回北京时间)。

    同时记录交给 _datetime_to_ms 的 datetime 是否带时区: naive 值经 .timestamp()
    按服务器本地时区解释, 只有带北京时区才与主机时区无关。
    """
    windows: list[tuple[datetime, datetime]] = []
    offsets: list = []

    class _FakeKlines:
        @staticmethod
        def batch(symbols, period, start_time=None, end_time=None, **kwargs):
            windows.append((
                datetime.fromtimestamp(start_time / 1000, tz=CN_TZ),
                datetime.fromtimestamp(end_time / 1000, tz=CN_TZ),
            ))
            return []

    class _FakeClient:
        klines = _FakeKlines

    real_to_ms = kline_sync._datetime_to_ms

    def _spy_to_ms(dt: datetime) -> int:
        offsets.append(dt.utcoffset())
        return real_to_ms(dt)

    monkeypatch.setattr(kline_sync, "_try_custom_minute", lambda *a, **kw: (None, True))
    monkeypatch.setattr(kline_sync, "get_client", lambda: _FakeClient())
    monkeypatch.setattr("app.data_providers.tickflow_provider._datetime_to_ms", _spy_to_ms)
    return windows, offsets


def test_sync_minute_batch_naive_window_is_beijing_wall_clock(monkeypatch):
    """naive 窗口按北京墙钟解释 (分钟 K 的落盘与接口口径), 不随服务器本地时区漂移。"""
    windows, offsets = _capture_tickflow_minute_batch(monkeypatch)

    kline_sync.sync_minute_batch(
        ["600000.SH"],
        start_time=datetime(2026, 8, 14, 9, 25),
        end_time=datetime(2026, 8, 14, 15, 5),
    )

    assert offsets and all(offset == timedelta(hours=8) for offset in offsets)
    assert windows == [(
        datetime(2026, 8, 14, 9, 25, tzinfo=CN_TZ),
        datetime(2026, 8, 14, 15, 5, tzinfo=CN_TZ),
    )]


def _minute_batch_request(local: pl.DataFrame) -> MagicMock:
    repo = MagicMock()
    repo.get_etf_symbol_set.return_value = set()
    repo.get_minute_batch.return_value = local
    capset = MagicMock()
    capset.has.return_value = True
    capset.limits.return_value = None
    request = MagicMock()
    request.app.state.repo = repo
    request.app.state.capabilities = capset
    return request


def test_minute_batch_endpoint_pull_windows_are_beijing_wall_clock(monkeypatch):
    """/api/kline/minute-batch 补拉: 全天窗口 09:25-15:05 与增量起点 (本地最后一根) 均为北京墙钟。

    端点用 naive 北京墙钟构造窗口; 若按服务器本地时区换算, UTC 主机上请求的是
    北京 17:25-23:05, 自选分时补拉恒为空。
    """
    from app.api import kline as kline_api

    windows, offsets = _capture_tickflow_minute_batch(monkeypatch)
    empty_symbol = "600000.SH"
    tail_symbol = "600519.SH"  # 本地有 09:31-09:33 连续三根 → 尾部落后, 从最后一根增量拉
    local = pl.DataFrame({
        "symbol": [tail_symbol] * 3,
        "datetime": [datetime(2026, 8, 14, 9, 31) + timedelta(minutes=i) for i in range(3)],
        "close": [10.0] * 3,
    })

    kline_api.get_minute_batch(
        _minute_batch_request(local),
        {"symbols": [empty_symbol, tail_symbol], "date": "2026-08-14"},
    )

    assert offsets and all(offset == timedelta(hours=8) for offset in offsets)
    assert sorted(windows) == [
        (datetime(2026, 8, 14, 9, 25, tzinfo=CN_TZ), datetime(2026, 8, 14, 15, 5, tzinfo=CN_TZ)),
        (datetime(2026, 8, 14, 9, 33, tzinfo=CN_TZ), datetime(2026, 8, 14, 15, 5, tzinfo=CN_TZ)),
    ]
