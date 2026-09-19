"""指数日 K 盘中必须注入当日实时蜡烛, 与股票/ETF 的 /api/kline/daily 同口径。

/api/index/daily 读完 parquet 直接返回, 不走 _maybe_inject_live_candle。
指数页和板块卡片用这个接口; 个股弹窗里的指数走 /api/kline/daily/latest,
而 _latest_live_candle 对 index 直接 return None。盘中指数日 K 停在上一交易日。
指数 live enriched 缓存已经由 quote_service flush/merge 维护
(test_repository_index / test_quote_index_merge)。
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices
from app.api.kline import router as kline_router

TODAY = date.today()
YDAY = TODAY - timedelta(days=1)


class _IndexRepo:
    def __init__(self) -> None:
        self.latest_calls: list[tuple[str, bool]] = []

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
        return pl.DataFrame({
            "symbol": ["000001.SH"],
            "date": [TODAY],
            "open": [3010.0],
            "high": [3050.0],
            "low": [3008.0],
            "close": [3040.0],
            "volume": [2.0],
            "amount": [2.0],
            "change_pct": [0.0116],
        }), TODAY

    def resolve_asset_type(self, symbol: str) -> str:
        return "index"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    # 已修路径比 cn_today(); 未修路径比 date.today()。TODAY 取 date.today(),
    # 再把 cn_today 钉成同一天, 两种判定都认为缓存是「今天」。
    monkeypatch.setattr(indices, "cn_today", lambda: TODAY, raising=False)
    from app.api import kline as kline_api
    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY, raising=False)


def test_index_daily_injects_live_candle(clock) -> None:
    repo = _IndexRepo()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=MagicMock())),
    )
    result = indices.get_index_daily(
        request, symbol="000001.SH", days=5, start_date=None, end_date=None,
    )
    dates = [str(r["date"])[:10] for r in result["rows"]]
    assert TODAY.isoformat() in dates, f"盘中必须带上当日实时K, 实际 {dates}"
    live = next(r for r in result["rows"] if str(r["date"])[:10] == TODAY.isoformat())
    assert live["close"] == 3040.0
    assert ("index", True) in repo.latest_calls


def test_kline_daily_latest_reads_index_cache(clock) -> None:
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
