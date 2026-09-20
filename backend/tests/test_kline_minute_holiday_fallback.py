"""个股分时 /minute 与自选分时 /minute_batch 的缺省日期 — 工作日休市按交易日探针回退。

两个端点不传 date 时默认看今天, 休市才回退到本地最近交易日。判据是:
周六/周日必回退; 工作日要到 15:30 之后且仍无今日日K才判为节假日。
于是国庆 (2026-10-01 周四) 这类工作日休市, 15:30 之前每一轮都去取当天的
空分区 → 本地为空 → 实时补拉 (休市返回空) → 分时图整个上午到下午空白。

指数分时 /api/index/minute (#308) 已改用 trading_day.is_trading_day() 的三态口径:
False = 确定休市才回退, None = 未知维持原判据。这里对齐同一口径。
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

HOLIDAY = date(2026, 10, 1)          # 周四, 国庆休市
LAST_SESSION = date(2026, 9, 30)      # 节前最后一个交易日
MORNING = datetime(2026, 10, 1, 10, 0, tzinfo=CN_TZ)
SYMBOL = "600519.SH"


def _session(day: date, symbol: str = SYMBOL) -> pl.DataFrame:
    start = datetime(day.year, day.month, day.day, 9, 31)
    return pl.DataFrame({
        "symbol": [symbol] * 240,
        "datetime": [start + timedelta(minutes=i) for i in range(240)],
        "close": [100.0] * 240,
    })


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch):
    """北京时间钉在国庆当天上午 10:00; 返回设置探针结论的函数。"""
    monkeypatch.setattr(kline_api, "cn_now", lambda: MORNING)
    monkeypatch.setattr(kline_api, "cn_today", lambda: HOLIDAY)
    monkeypatch.setattr(kline_api, "in_continuous_session", lambda: True)
    monkeypatch.setattr(preferences, "get_minute_batch_compress", lambda: False)
    fetch = MagicMock(return_value=pl.DataFrame())
    monkeypatch.setattr(kline_api.kline_sync, "fetch_minute_single", fetch)
    sync = MagicMock(return_value=pl.DataFrame())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync)

    def set_verdict(verdict: bool | None) -> None:
        monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: verdict)

    return set_verdict, fetch, sync


# ---------- /minute_batch (自选列表分时小图) ----------


def _batch_request() -> MagicMock:
    repo = MagicMock()
    repo.get_etf_symbol_set.return_value = set()
    sessions = {LAST_SESSION: _session(LAST_SESSION)}
    repo.get_minute_batch.side_effect = (
        lambda syms, day, asset_type="stock": sessions.get(day, pl.DataFrame())
    )
    repo.latest_minute_date_global.return_value = LAST_SESSION
    repo.latest_daily_date.return_value = LAST_SESSION
    capset = MagicMock()
    capset.has.return_value = True
    capset.limits.return_value = None
    request = MagicMock()
    request.app.state.repo = repo
    request.app.state.capabilities = capset
    request.headers = {}
    return request


def test_minute_batch_on_weekday_holiday_falls_back_to_last_session(market) -> None:
    set_verdict, _, sync = market
    set_verdict(False)
    request = _batch_request()

    payload = kline_api.get_minute_batch(request, {"symbols": [SYMBOL]})

    assert set(payload["data"]) == {SYMBOL}
    rows = payload["data"][SYMBOL]
    assert len(rows) == 240
    assert rows[0]["datetime"].date() == LAST_SESSION
    assert request.app.state.repo.get_minute_batch.call_args.args[1] == LAST_SESSION
    sync.assert_not_called()  # 不再对休市当天发起补拉


@pytest.mark.parametrize("verdict", [True, None])
def test_minute_batch_trading_or_unknown_day_keeps_today(market, verdict) -> None:
    set_verdict, _, _ = market
    set_verdict(verdict)
    request = _batch_request()

    kline_api.get_minute_batch(request, {"symbols": [SYMBOL]})

    assert request.app.state.repo.get_minute_batch.call_args.args[1] == HOLIDAY


def test_minute_batch_explicit_date_is_not_replaced(market) -> None:
    set_verdict, _, _ = market
    set_verdict(False)
    request = _batch_request()

    kline_api.get_minute_batch(request, {"symbols": [SYMBOL], "date": "2026-10-01"})

    assert request.app.state.repo.get_minute_batch.call_args.args[1] == HOLIDAY


# ---------- /minute (个股详情分时) ----------


class _FakeRepo:
    def __init__(self) -> None:
        # /minute 现读取 repo.store.data_dir 判断分钟基准标记 (未标记目录即旧行为)
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
        if trade_date == LAST_SESSION:
            return _session(LAST_SESSION).drop("symbol")
        return pl.DataFrame()

    def latest_minute_date(self, symbol, asset_type="stock") -> date:
        return LAST_SESSION

    def latest_daily_date(self) -> date:
        return LAST_SESSION


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(kline_api.router)
    app.state.repo = _FakeRepo()
    app.state.capabilities = CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()})
    return TestClient(app)


@pytest.mark.parametrize("live", [0, 1])
def test_minute_on_weekday_holiday_falls_back_to_last_session(market, live) -> None:
    set_verdict, fetch, _ = market
    set_verdict(False)

    body = _client().get("/api/kline/minute", params={"symbol": SYMBOL, "live": live}).json()

    assert body["date"] == "2026-09-30"
    assert body["source"] == "local"
    assert len(body["rows"]) == 240
    fetch.assert_not_called()


@pytest.mark.parametrize("verdict", [True, None])
def test_minute_trading_or_unknown_day_keeps_today(market, verdict) -> None:
    set_verdict, _, _ = market
    set_verdict(verdict)

    body = _client().get("/api/kline/minute", params={"symbol": SYMBOL}).json()

    assert body["date"] == "2026-10-01"


def test_minute_explicit_date_is_not_replaced(market) -> None:
    set_verdict, _, _ = market
    set_verdict(False)

    body = _client().get("/api/kline/minute", params={"symbol": SYMBOL, "date": "2026-10-01"}).json()

    assert body["date"] == "2026-10-01"
