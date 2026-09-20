"""指数日 K 盘中必须注入当日实时蜡烛, 与股票/ETF 的 /api/kline/daily 同口径。

/api/index/daily 读完 parquet 直接返回, 不走 _maybe_inject_live_candle。
指数页和板块卡片用这个接口; 个股弹窗里的指数走 /api/kline/daily/latest,
而 _latest_live_candle 对 index 直接 return None。盘中指数日 K 停在上一交易日。
指数 live enriched 缓存由 quote_service 每轮 merge/flush 维护, 不依赖指数监控规则
(test_repository_index / test_quote_index_merge / test_index_live_write_without_monitor_rules)。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices
from app.api.kline import router as kline_router

# 钉死的北京日期 (#369 后读侧守卫是 cn_today, 测试必须与服务器本地 date.today() 解耦)
TODAY = date(2026, 3, 2)
YDAY = date(2026, 3, 1)


def _live_index_frame(day: date) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SH"],
        "date": [day],
        "open": [3010.0],
        "high": [3050.0],
        "low": [3008.0],
        "close": [3040.0],
        "volume": [2.0],
        "amount": [2.0],
        "change_pct": [0.0116],
    })


class _IndexRepo:
    def __init__(
        self,
        latest: tuple[pl.DataFrame, date | None] | None = None,
    ) -> None:
        self.latest_calls: list[tuple[str, bool]] = []
        self._latest = latest if latest is not None else (_live_index_frame(TODAY), TODAY)

    def get_index_instruments(self) -> pl.DataFrame:
        return pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]})

    def get_index_daily(self, symbol, start, end, columns=None) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": ["000001.SH"],
            "date": [YDAY],
            "open": [3000.0],
            "high": [3010.0],
            "low": [2990.0],
            "close": [3005.0],
            "volume": [1.0],
            "amount": [1.0],
        })

    def get_enriched_latest_asset(self, asset_type: str, refresh: bool = True):
        self.latest_calls.append((asset_type, refresh))
        return self._latest

    def resolve_asset_type(self, symbol: str) -> str:
        return "index"


def _index_request(repo: _IndexRepo):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=MagicMock())),
    )


# #369 已合入: 读侧守卫是 cn_today, 注入路径测试统一钉 kline_api.cn_today = TODAY


def test_index_daily_injects_live_candle(monkeypatch) -> None:
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY)
    repo = _IndexRepo()
    result = indices.get_index_daily(
        _index_request(repo), symbol="000001.SH", days=5, start_date=None, end_date=None,
    )
    dates = [str(r["date"])[:10] for r in result["rows"]]
    assert TODAY.isoformat() in dates, f"盘中必须带上当日实时K, 实际 {dates}"
    live = next(r for r in result["rows"] if str(r["date"])[:10] == TODAY.isoformat())
    assert live["close"] == 3040.0
    assert live["change_pct"] == 0.0116
    assert ("index", True) in repo.latest_calls


@pytest.mark.parametrize(
    "latest",
    [
        (pl.DataFrame(), None),
        (_live_index_frame(YDAY), YDAY),
    ],
    ids=["empty", "stale"],
)
def test_index_daily_skips_live_candle_when_cache_unusable(monkeypatch, latest) -> None:
    """enriched 为空或日期非今日时, 历史行原样返回, 不追加蜡烛。"""
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY)
    repo = _IndexRepo(latest=latest)
    result = indices.get_index_daily(
        _index_request(repo), symbol="000001.SH", days=5, start_date=None, end_date=None,
    )
    dates = [str(r["date"])[:10] for r in result["rows"]]
    assert dates == [YDAY.isoformat()]
    assert TODAY.isoformat() not in dates


def test_kline_daily_latest_reads_index_cache(monkeypatch) -> None:
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY)
    repo = _IndexRepo()
    app = FastAPI()
    app.include_router(kline_router)
    app.state.repo = repo
    app.state.quote_service = SimpleNamespace(get_enriched_today=lambda: (pl.DataFrame(), None))
    client = TestClient(app)
    response = client.get("/api/kline/daily/latest", params={"symbol": "000001.SH"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "live"
    assert body["row"]["close"] == 3040.0
    assert repo.latest_calls == [("index", False)]


def test_index_daily_default_end_is_beijing_today(monkeypatch) -> None:
    """/api/index/daily 未传 end_date 时窗口右端必须是北京今天。

    #371 给指数日 K 补了实时注入, 但读 parquet 的默认截止日仍是 date.today()。
    缓存冷时注入为空, 窗口把北京当日的官方指数 K 排除。
    raising=False: 未修复代码没有在这条路径调用 cn_today。
    """
    from datetime import date as _date

    from app.api import kline as kline_api

    captured: list[tuple] = []
    repo = _IndexRepo()
    orig = repo.get_index_daily

    def _wrap(symbol, start, end, columns=None):
        captured.append((start, end))
        return orig(symbol, start, end, columns)

    repo.get_index_daily = _wrap  # type: ignore[method-assign]
    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY, raising=False)
    monkeypatch.setattr(indices, "cn_today", lambda: TODAY, raising=False)

    indices.get_index_daily(
        _index_request(repo), symbol="000001.SH", days=5, start_date=None, end_date=None,
    )
    assert captured, "应查询指数日K"
    _start, end = captured[0]
    assert end == TODAY, f"窗口右端必须是北京日期 {TODAY}, 实际 {end} (服务器本地 {_date.today()})"
