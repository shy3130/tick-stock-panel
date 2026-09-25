"""个股详情日 K 最新行接口测试。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kline import router


class _FakeQuoteService:
    def __init__(self, frame: pl.DataFrame, trade_date: date | None) -> None:
        self._frame = frame
        self._trade_date = trade_date

    def get_enriched_today(self):
        return self._frame, self._trade_date


class _FakeRepo:
    def __init__(
        self,
        asset_type: str = "stock",
        latest_asset: tuple[pl.DataFrame, date | None] | None = None,
    ) -> None:
        self.asset_type = asset_type
        self.latest_asset = latest_asset
        self.latest_asset_calls = 0
        self.latest_asset_refresh: bool | None = None

    def resolve_asset_type(self, symbol: str) -> str:
        return self.asset_type

    def get_enriched_latest_asset(self, asset_type: str, refresh: bool = True):
        self.latest_asset_calls += 1
        self.latest_asset_refresh = refresh
        if self.latest_asset is not None:
            return self.latest_asset
        raise AssertionError("stock latest row must use QuoteService's memory cache")


def _client(
    frame: pl.DataFrame,
    trade_date: date | None,
    *,
    asset_type: str = "stock",
    latest_asset: tuple[pl.DataFrame, date | None] | None = None,
) -> tuple[TestClient, _FakeRepo]:
    repo = _FakeRepo(asset_type, latest_asset)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    app.state.quote_service = _FakeQuoteService(frame, trade_date)
    return TestClient(app), repo


def _live_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date.today()],
        "open": [10.0],
        "high": [10.8],
        "low": [9.9],
        "close": [10.6],
        "volume": [123_456.0],
        "amount": [1_234_560.0],
        "ma5": [10.2],
        "ma120": [10.1],
        "ma200": [9.9],
        "signal_limit_up": [False],
    })


def test_daily_latest_returns_only_current_memory_row(monkeypatch) -> None:
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: date.today())
    client, repo = _client(_live_frame(), date.today())

    response = client.get("/api/kline/daily/latest", params={"symbol": "600000.SH"})

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "600000.SH"
    assert body["source"] == "live"
    assert body["row"] == {
        "symbol": "600000.SH",
        "date": date.today().isoformat(),
        "open": 10.0,
        "high": 10.8,
        "low": 9.9,
        "close": 10.6,
        "volume": 123_456.0,
        "amount": 1_234_560.0,
        "change_pct": None,
        "ma5": 10.2,
        "ma120": 10.1,
        "ma200": 9.9,
        "is_live": True,
    }
    assert repo.latest_asset_calls == 0


def test_daily_latest_returns_none_for_stale_cache(monkeypatch) -> None:
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: date.today())
    client, _ = _client(_live_frame(), date.today() - timedelta(days=1))

    response = client.get("/api/kline/daily/latest", params={"symbol": "600000.SH"})

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "600000.SH",
        "row": None,
        "source": "none",
    }


def test_daily_latest_returns_none_when_symbol_is_missing(monkeypatch) -> None:
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: date.today())
    client, _ = _client(_live_frame(), date.today())

    response = client.get("/api/kline/daily/latest", params={"symbol": "600001.SH"})

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "600001.SH",
        "row": None,
        "source": "none",
    }


def test_daily_latest_uses_etf_enriched_cache(monkeypatch) -> None:
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: date.today())
    etf = _live_frame().with_columns(pl.lit("510300.SH").alias("symbol"))
    client, repo = _client(
        pl.DataFrame(),
        None,
        asset_type="etf",
        latest_asset=(etf, date.today()),
    )

    response = client.get("/api/kline/daily/latest", params={"symbol": "510300.SH"})

    assert response.status_code == 200
    assert response.json()["row"]["close"] == 10.6
    assert response.json()["source"] == "live"
    assert repo.latest_asset_calls == 1
    assert repo.latest_asset_refresh is False


def test_daily_latest_uses_index_enriched_cache(monkeypatch) -> None:
    """指数日K的当日实时行走独立 index enriched 缓存, 不能直接 return None。"""
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: date.today())
    idx = _live_frame().with_columns(pl.lit("000001.SH").alias("symbol"))
    client, repo = _client(
        pl.DataFrame(),
        None,
        asset_type="index",
        latest_asset=(idx, date.today()),
    )

    response = client.get("/api/kline/daily/latest", params={"symbol": "000001.SH"})

    assert response.status_code == 200
    body = response.json()
    assert body["row"] is not None
    assert body["row"]["close"] == 10.6
    assert body["source"] == "live"
    assert repo.latest_asset_calls == 1
    assert repo.latest_asset_refresh is False
BJ = date(2026, 3, 2)  # 钉死的北京日期, 不会碰巧等于跑测试那天的 date.today()


def _frame_on(day: date, symbol: str = "600000.SH") -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "date": [day],
        "open": [10.0],
        "high": [10.8],
        "low": [9.9],
        "close": [10.6],
        "volume": [123_456.0],
        "amount": [1_234_560.0],
        "ma5": [10.2],
        "signal_limit_up": [False],
    })


def _request(frame: pl.DataFrame, trade_date: date):
    from types import SimpleNamespace

    repo = _FakeRepo()
    qs = _FakeQuoteService(frame, trade_date)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, quote_service=qs)))


def test_live_candle_uses_beijing_today_not_server_local(monkeypatch) -> None:
    """缓存日期是北京今天时必须注入; 不能拿服务器本地 date.today() 去比较。

    raising=False: 未修复代码没有在这条路径调用 cn_today, 钉了也不会被用到,
    比较仍走 date.today() → 与 2026-03-02 不等 → 返回 None。
    """
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: BJ, raising=False)
    row = kline_api._latest_live_candle(_request(_frame_on(BJ), BJ), "600000.SH", "stock")
    assert row is not None
    assert row["close"] == 10.6
    assert row["date"] == str(BJ)


def test_live_candle_stale_vs_beijing_today(monkeypatch) -> None:
    """缓存停在北京昨天时不注入。"""
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: BJ, raising=False)
    row = kline_api._latest_live_candle(
        _request(_frame_on(date(2026, 3, 1)), date(2026, 3, 1)), "600000.SH", "stock",
    )
    assert row is None


def test_remote_daily_fallback_warms_ma200(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.api import kline as kline_api

    requested: dict[str, int] = {}
    raw = pl.DataFrame({
        "symbol": ["600000.SH"] * 320,
        "date": [date(2025, 1, 1) + timedelta(days=i) for i in range(320)],
        "open": [10.0 + i * 0.01 for i in range(320)],
        "high": [10.1 + i * 0.01 for i in range(320)],
        "low": [9.9 + i * 0.01 for i in range(320)],
        "close": [10.0 + i * 0.01 for i in range(320)],
        "volume": [1000.0 + i for i in range(320)],
        "amount": [10_000.0 + i * 10 for i in range(320)],
    })

    def fake_sync(symbols, count):
        requested["count"] = count
        return raw

    class _Repo:
        def resolve_asset_type(self, symbol: str) -> str:
            return "stock"

        def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
            return pl.DataFrame()

        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame()

    monkeypatch.setattr(kline_api.kline_sync, "sync_daily_batch", fake_sync)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=_Repo(), quote_service=None, capabilities=None,
    )))

    response = kline_api.get_daily(
        request, symbol="600000.SH", days=120, start_date=None, end_date=None, ext_columns=None,
    )

    assert requested["count"] == 320
    assert len(response["rows"]) == 120
    assert all(row["ma200"] is not None for row in response["rows"])


def test_get_daily_default_end_is_beijing_today(monkeypatch) -> None:
    """/api/kline/daily 未传 end_date 时窗口右端必须是北京今天。

    实时注入只在内存缓存命中时补当日 K。缓存冷 (进程刚起 / 盘后重启) 时,
    parquet 里的当日行能否进结果完全取决于查询窗口。未修复代码用
    date.today(): 美西盘中、UTC 北京 00:00-08:00 会把当日官方 K 排除。
    raising=False: 未修复代码没有调用 cn_today, 钉了也不会被用到。
    """
    from types import SimpleNamespace

    from app.api import kline as kline_api

    captured: list[tuple[date, date]] = []

    class _Repo:
        def resolve_asset_type(self, symbol: str) -> str:
            return "stock"

        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame({
                "symbol": ["600000.SH"], "name": ["浦发银行"],
                "total_shares": [1.0], "float_shares": [1.0],
            })

        def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
            captured.append((start, end))
            return pl.DataFrame({
                "symbol": ["600000.SH"], "date": [BJ],
                "open": [10.0], "high": [10.6], "low": [9.9], "close": [10.6],
                "volume": [1.0], "amount": [1.0],
            })

    monkeypatch.setattr(kline_api, "cn_today", lambda: BJ, raising=False)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=_Repo(), quote_service=None, capabilities=None,
    )))
    kline_api.get_daily(req, symbol="600000.SH", days=120, start_date=None, end_date=None, ext_columns=None)
    assert captured, "应查询日K"
    _start, end = captured[0]
    assert end == BJ, f"窗口右端必须是北京日期 {BJ}, 实际 {end} (服务器本地 {date.today()})"
