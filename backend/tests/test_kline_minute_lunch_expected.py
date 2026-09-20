"""个股分时 /minute 与自选分时 /minute-batch 在午休 11:31-12:00 的期望根数。

两个端点按北京时刻估算当日「应有」的分钟K根数, 本地根数够了就直接用本地,
不够才去数据源实时拉取。上午分支写成了 `h < 12 or (h == 12 and m == 0)`,
于是 11:31-12:00 仍按 `(h - 9) * 60 + m - 30` 继续增长 (121..150), 而上午
只有 120 根 (09:31-11:30)。本地完整的上午数据在这段时间被判为「不完整」:

- /minute: 走 fetch_minute_single 实时拉取; 拉空时响应 rows=[]、source="none",
  前端 minuteRefetchInterval 见 source="none" 即停止轮询, 分时图空白到重新打开。
- /minute-batch: 被归为「尾部落后」, 每轮对所有自选标的发起增量补拉; 补拉为空时
  该标的不出现在响应里。

午休应与 market_time.trading_minutes_elapsed_from_dt 同口径: 保持上午累计 120。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline as kline_api
from app.market_time import CN_TZ
from app.services import preferences, trading_day
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

TODAY = date(2026, 9, 15)  # 周二, 交易日
SYMBOL = "600519.SH"


def _morning(symbol: str = SYMBOL, bars: int = 120) -> pl.DataFrame:
    """当日上午从 09:31 起连续 bars 根分钟K (120 根 = 完整上午, 到 11:30)。"""
    start = datetime(TODAY.year, TODAY.month, TODAY.day, 9, 31)
    return pl.DataFrame({
        "symbol": [symbol] * bars,
        "datetime": [start + timedelta(minutes=i) for i in range(bars)],
        "close": [100.0] * bars,
    })


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """钉住交易日与探针, 数据源补拉一律返回空; 返回 (设置北京时刻的函数, 单股拉取 mock, 批量拉取 mock)。"""
    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY)
    monkeypatch.setattr(kline_api, "in_continuous_session", lambda: False)
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(preferences, "get_minute_batch_compress", lambda: False)
    fetch = MagicMock(return_value=pl.DataFrame())
    monkeypatch.setattr(kline_api.kline_sync, "fetch_minute_single", fetch)
    sync = MagicMock(return_value=pl.DataFrame())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync)

    def set_time(hour: int, minute: int) -> None:
        now = datetime(TODAY.year, TODAY.month, TODAY.day, hour, minute, tzinfo=CN_TZ)
        monkeypatch.setattr(kline_api, "cn_now", lambda: now)

    return set_time, fetch, sync


# 午休时段: 上午 120 根已是完整数据
LUNCH_TIMES = [(11, 35), (11, 50), (12, 0)]


# ---------- /minute (个股详情分时) ----------


class _FakeRepo:
    def __init__(self, bars: int = 120) -> None:
        self._bars = bars
        # /minute 与 /minute-batch 读取 repo.store.data_dir 判断分钟基准标记
        import tempfile
        from types import SimpleNamespace
        from pathlib import Path
        self.store = SimpleNamespace(data_dir=Path(tempfile.mkdtemp()))

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame({"symbol": [], "name": [], "total_shares": [], "float_shares": []})

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        return pl.DataFrame({"date": [], "close": []})

    def get_minute(self, symbol, trade_date, asset_type="stock") -> pl.DataFrame:
        if trade_date == TODAY:
            return _morning(bars=self._bars).drop("symbol")
        return pl.DataFrame()


def _client(bars: int = 120) -> TestClient:
    app = FastAPI()
    app.include_router(kline_api.router)
    app.state.repo = _FakeRepo(bars)
    app.state.capabilities = CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()})
    return TestClient(app)


@pytest.mark.parametrize(("hour", "minute"), LUNCH_TIMES)
@pytest.mark.parametrize("live", [0, 1])
def test_minute_lunch_break_serves_complete_morning_from_local(clock, hour, minute, live) -> None:
    set_time, fetch, _ = clock
    set_time(hour, minute)

    body = _client().get("/api/kline/minute", params={"symbol": SYMBOL, "live": live}).json()

    assert (body["source"], len(body["rows"])) == ("local", 120)
    fetch.assert_not_called()


@pytest.mark.parametrize(("hour", "minute", "bars"), [(10, 30, 60), (13, 30, 150)])
def test_minute_trading_session_expected_unchanged(clock, hour, minute, bars) -> None:
    """连续竞价时段的估算不变: 本地根数跟上时刻即用本地。"""
    set_time, fetch, _ = clock
    set_time(hour, minute)

    body = _client(bars).get("/api/kline/minute", params={"symbol": SYMBOL}).json()

    assert (body["source"], len(body["rows"])) == ("local", bars)
    fetch.assert_not_called()


def test_minute_lunch_break_still_pulls_when_morning_incomplete(clock) -> None:
    """午休时本地上午明显不全 (只有 60 根) 仍走实时拉取。"""
    set_time, fetch, _ = clock
    set_time(11, 45)

    _client(60).get("/api/kline/minute", params={"symbol": SYMBOL})

    fetch.assert_called_once()


# ---------- /minute-batch (自选列表分时小图) ----------


def _batch_request(bars: int = 120) -> MagicMock:
    repo = MagicMock()
    repo.get_etf_symbol_set.return_value = set()
    repo.get_minute_batch.side_effect = (
        lambda syms, day, asset_type="stock": _morning(bars=bars) if day == TODAY else pl.DataFrame()
    )
    capset = MagicMock()
    capset.has.return_value = True
    capset.limits.return_value = None
    request = MagicMock()
    request.app.state.repo = repo
    request.app.state.capabilities = capset
    request.headers = {}
    return request


@pytest.mark.parametrize(("hour", "minute"), LUNCH_TIMES)
def test_minute_batch_lunch_break_serves_complete_morning_from_local(clock, hour, minute) -> None:
    set_time, _, sync = clock
    set_time(hour, minute)

    payload = kline_api.get_minute_batch(_batch_request(), {"symbols": [SYMBOL]})

    assert len(payload["data"].get(SYMBOL, [])) == 120
    sync.assert_not_called()


@pytest.mark.parametrize(("hour", "minute", "bars"), [(10, 30, 60), (13, 30, 150)])
def test_minute_batch_trading_session_expected_unchanged(clock, hour, minute, bars) -> None:
    set_time, _, sync = clock
    set_time(hour, minute)

    payload = kline_api.get_minute_batch(_batch_request(bars), {"symbols": [SYMBOL]})

    assert len(payload["data"].get(SYMBOL, [])) == bars
    sync.assert_not_called()


def test_minute_batch_lunch_break_still_pulls_when_morning_incomplete(clock) -> None:
    set_time, _, sync = clock
    set_time(11, 45)

    kline_api.get_minute_batch(_batch_request(60), {"symbols": [SYMBOL]})

    sync.assert_called_once()
