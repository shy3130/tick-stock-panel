"""分钟 K datetime 北京墙钟契约测试。

契约 (CONTRIBUTING §3.3): kline_minute.datetime 必须是北京墙钟 naive。
守卫 _enforce_minute_beijing_wallclock 在两个源头入口强制:
- _normalize_minute (TickFlow 帧, timestamp 毫秒为 UTC 基准)
- _try_custom_minute (插件/自定义源帧)

覆盖: 显式转换 / 北京墙钟直通 / UTC 特征自愈 +8 / tz-aware 换算 /
fail-closed 拒收 / 路由级契约违规回退 TickFlow。
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.services import kline_sync


def _minute_frame(datetimes: list, symbol: str = "600519.SH") -> pl.DataFrame:
    n = len(datetimes)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "datetime": datetimes,
        "open": [10.0] * n,
        "high": [10.5] * n,
        "low": [9.5] * n,
        "close": [10.2] * n,
        "volume": [100.0] * n,
        "amount": [1020.0] * n,
    })


def _beijing_day() -> list[datetime]:
    """一个正常交易日墙钟样本: 开盘/午盘首/收盘。"""
    return [
        datetime(2026, 1, 15, 9, 30),
        datetime(2026, 1, 15, 13, 0),
        datetime(2026, 1, 15, 15, 0),
    ]


# ---------- TickFlow 路径: timestamp 毫秒 (UTC 基准) → 北京墙钟 ----------

def test_tickflow_timestamp_normalizes_to_beijing_wallclock():
    """09:30 北京 = 01:30 UTC; SDK 帧 timestamp 毫秒归一后必须回到 09:30。"""
    ts_ms = [
        int(datetime(2026, 1, 15, 1, 30, tzinfo=UTC).timestamp() * 1000),   # 09:30 北京
        int(datetime(2026, 1, 15, 5, 0, tzinfo=UTC).timestamp() * 1000),    # 13:00 北京
        int(datetime(2026, 1, 15, 7, 0, tzinfo=UTC).timestamp() * 1000),    # 15:00 北京
    ]
    df = pl.DataFrame({
        "symbol": ["600519.SH"] * 3,
        "timestamp": ts_ms,
        "open": [10.0] * 3, "high": [10.5] * 3,
        "low": [9.5] * 3, "close": [10.2] * 3,
        "volume": [100.0] * 3, "amount": [1020.0] * 3,
    })
    out = kline_sync._normalize_minute(df)
    assert out["datetime"].to_list() == _beijing_day()


def test_tickflow_timestamp_partial_day_lunch_unaffected():
    """午间 11:30 (03:30 UTC) 同样正确归一, 不被误判为越界。"""
    df = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "timestamp": [int(datetime(2026, 1, 15, 3, 30, tzinfo=UTC).timestamp() * 1000)],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0],
    })
    out = kline_sync._normalize_minute(df)
    assert out["datetime"].to_list() == [datetime(2026, 1, 15, 11, 30)]


# ---------- 守卫: 各口径分类 ----------

def test_guard_beijing_naive_passthrough():
    df = _minute_frame(_beijing_day())
    out = kline_sync._enforce_minute_beijing_wallclock(df, source="t")
    assert out["datetime"].to_list() == _beijing_day()


def test_guard_utc_naive_selfhealed_plus8():
    """01:30/05:00/07:00 (UTC 墙钟特征) → 自动 +8 → 09:30/13:00/15:00。"""
    df = _minute_frame([
        datetime(2026, 1, 15, 1, 30),
        datetime(2026, 1, 15, 5, 0),
        datetime(2026, 1, 15, 7, 0),
    ])
    out = kline_sync._enforce_minute_beijing_wallclock(df, source="t")
    assert out["datetime"].to_list() == _beijing_day()


def test_guard_selfheal_is_idempotent():
    df = _minute_frame([datetime(2026, 1, 15, 1, 30), datetime(2026, 1, 15, 3, 0)])
    once = kline_sync._enforce_minute_beijing_wallclock(df, source="t")
    twice = kline_sync._enforce_minute_beijing_wallclock(once, source="t")
    assert once["datetime"].to_list() == twice["datetime"].to_list()


def test_guard_tzaware_utc_converted():
    """tz-aware UTC 01:30 → 北京墙钟 09:30 (naive)。"""
    df = _minute_frame([datetime(2026, 1, 15, 1, 30, tzinfo=UTC)])
    out = kline_sync._enforce_minute_beijing_wallclock(df, source="t")
    assert out["datetime"].dtype == pl.Datetime("us")
    assert out["datetime"].to_list() == [datetime(2026, 1, 15, 9, 30)]


def test_guard_tzaware_shanghai_converted():
    """tz-aware +08:00 09:30 → 北京墙钟 09:30 (naive), 数值不变。"""
    df = _minute_frame([datetime(2026, 1, 15, 9, 30, tzinfo=_shanghai_tz())])
    out = kline_sync._enforce_minute_beijing_wallclock(df, source="t")
    assert out["datetime"].to_list() == [datetime(2026, 1, 15, 9, 30)]


def _shanghai_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Asia/Shanghai")


def test_guard_unrecognized_convention_fails_closed():
    """21:30/22:15 (境外墙钟特征) 既非北京时段也非 UTC 平移 → 拒收。"""
    df = _minute_frame([datetime(2026, 1, 15, 21, 30), datetime(2026, 1, 15, 22, 15)])
    with pytest.raises(ValueError, match="口径无法识别"):
        kline_sync._enforce_minute_beijing_wallclock(df, source="t")


def test_guard_all_null_datetimes_passthrough():
    """全 null datetime 维持原行为 (下游落盘过滤), 不误伤。"""
    df = _minute_frame([None, None])
    out = kline_sync._enforce_minute_beijing_wallclock(df, source="t")
    assert out.height == 2
    assert out["datetime"].null_count() == 2


def test_guard_string_datetimes_classified_after_parse():
    """trade_time 字符串路径: 先解析再分类 (UTC 特征串同样自愈)。"""
    df = pl.DataFrame({
        "symbol": ["600519.SH"],
        "trade_time": ["2026-01-15 01:30:00"],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0],
    }).rename({"trade_time": "datetime"})
    out = kline_sync._enforce_minute_beijing_wallclock(df, source="t")
    assert out["datetime"].to_list() == [datetime(2026, 1, 15, 9, 30)]


# ---------- 路由级: 自定义源契约违规 → fail-closed ----------

def _setup_custom_provider(monkeypatch, provider: object) -> None:
    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "mock_src")
    provider.config = MagicMock(datasets={"minute": {}})
    monkeypatch.setattr("app.data_providers.custom.names", lambda: {"mock_src"})
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda name, ds: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)


def test_custom_provider_utc_frame_selfhealed(monkeypatch):
    """插件返回 UTC 墙钟帧 → 路由层守卫 +8 后下发, 不回退。"""
    mock_provider = MagicMock()
    mock_provider.get_minute = MagicMock(return_value=_minute_frame(
        [datetime(2026, 1, 15, 1, 30), datetime(2026, 1, 15, 5, 0)]))
    _setup_custom_provider(monkeypatch, mock_provider)

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"], datetime(2026, 1, 15, 9, 25), datetime(2026, 1, 15, 15, 5),
        asset_type="stock",
    )
    assert fallback is False
    assert df["datetime"].to_list() == [datetime(2026, 1, 15, 9, 30), datetime(2026, 1, 15, 13, 0)]


def test_custom_provider_garbage_datetime_fails_closed(monkeypatch):
    """插件返回无法识别口径 → fail-closed,不请求其他数据源。"""
    mock_provider = MagicMock()
    mock_provider.get_minute = MagicMock(return_value=_minute_frame(
        [datetime(2026, 1, 15, 21, 30)]))
    _setup_custom_provider(monkeypatch, mock_provider)

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"], datetime(2026, 1, 15, 9, 25), datetime(2026, 1, 15, 15, 5),
        asset_type="stock",
    )
    assert fallback is False
    assert df is not None and df.is_empty()
