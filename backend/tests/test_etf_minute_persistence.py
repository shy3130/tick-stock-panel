"""ETF 分钟补齐必须写入 ETF 仓库，并能被列表和详情立即读回。"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import kline as kline_api
from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 11, 5)
ETF = "159558.SZ"
STOCK = "600519.SH"


def _bars(symbol: str, day: date = DAY) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol, symbol],
        "datetime": [datetime.combine(day, datetime.min.time()).replace(hour=9, minute=minute) for minute in (31, 32)],
        "open": [0.75, 0.76], "high": [0.76, 0.77], "low": [0.74, 0.75],
        "close": [0.76, 0.77], "volume": [100.0, 200.0], "amount": [76.0, 154.0],
    })


@pytest.fixture
def market(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: {ETF})
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: set())
    capset = CapabilitySet()
    capset.grant(Cap.KLINE_MINUTE_BATCH)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=capset)), headers={})
    monkeypatch.setattr(kline_api, "_minute_allowed", lambda _: True)
    monkeypatch.setattr(kline_api, "cn_today", lambda: DAY)
    monkeypatch.setattr(kline_api, "cn_now", lambda: datetime(2026, 11, 5, 9, 32))
    monkeypatch.setattr(kline_api.trading_day, "is_trading_day", lambda: True)
    monkeypatch.setattr(kline_sync, "_selected_minute_provider_name", lambda: "test")
    monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda _: (object(), False, None))
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)
    calls = []

    def fetch(symbols, *, asset_type, on_segment, **kwargs):
        calls.append((list(symbols), asset_type))
        on_segment(pl.concat([_bars(symbol) for symbol in symbols]))
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)
    return repo, request, calls


def test_etf_detail_sync_is_visible_to_detail_and_list(market):
    repo, request, calls = market
    result = asyncio.run(kline_api.sync_minute_single(request, {"symbol": ETF, "days": 1}))

    assert result["rows"] == 2
    assert calls == [([ETF], "etf")]
    assert (repo.store.data_dir / "kline_etf_minute" / f"date={DAY}" / "part.parquet").exists()
    assert not list((repo.store.data_dir / "kline_minute").rglob("*.parquet"))
    history = kline_api.get_minute_range(request, ETF, 1)
    assert history["asset_type"] == "etf"
    assert history["sessions"][0]["rows"][-1]["close"] == 0.77
    latest = kline_api.get_minute(request, ETF, trade_date=DAY, live=False)
    assert latest["source"] == "local"
    assert len(latest["rows"]) == 2
    batch = json.loads(kline_api.get_minute_batch(request, {"symbols": [ETF], "date": str(DAY)}).body)
    assert len(batch["data"][ETF]) == 2
    assert repo.latest_minute_date(ETF, asset_type="etf") == DAY
    assert calls == [([ETF], "etf")]


def test_mixed_minute_sync_keeps_asset_stores_separate(market):
    repo, request, calls = market
    written = kline_sync.sync_and_persist_minute([STOCK, ETF], repo, request.app.state.capabilities, force_full_days=True)

    assert written == 4
    assert calls == [([STOCK], "stock"), ([ETF], "etf")]
    assert repo.get_minute(STOCK, DAY)["symbol"].unique().to_list() == [STOCK]
    assert repo.get_minute(ETF, DAY, asset_type="etf")["symbol"].unique().to_list() == [ETF]
    assert repo.get_minute(ETF, DAY).is_empty()


def test_partial_etf_sync_is_readable_and_retry_preserves_stock_data(market, monkeypatch):
    repo, request, _ = market
    root = repo.store.data_dir
    kline_sync._write_minute_partition(_bars(STOCK), root / "kline_minute")
    stock_file = root / "kline_minute" / f"date={DAY}" / "part.parquet"
    stock_bytes = stock_file.read_bytes()
    fetch = kline_sync.sync_minute_batch

    def interrupted(symbols, *, on_segment, **kwargs):
        on_segment(_bars(ETF))
        raise RuntimeError("后续批次中断")

    monkeypatch.setattr(kline_sync, "sync_minute_batch", interrupted)
    with pytest.raises(RuntimeError, match="后续批次中断"):
        asyncio.run(kline_api.sync_minute_single(request, {"symbol": ETF, "days": 1}))

    assert repo.latest_minute_date(ETF, asset_type="etf") == DAY
    assert len(kline_api.get_minute_range(request, ETF, 1)["sessions"]) == 1
    monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)
    # 失败有 1 秒冷却，阻止多窗口立刻反复冲击上游；冷却后仍可正常重试。
    time.sleep(1.05)
    asyncio.run(kline_api.sync_minute_single(request, {"symbol": ETF, "days": 1}))
    assert repo.get_minute(ETF, DAY, asset_type="etf").height == 2
    assert stock_file.read_bytes() == stock_bytes


@pytest.mark.parametrize("extend_backward", [False, True])
def test_etf_sync_uses_its_own_minute_window(market, monkeypatch, extend_backward):
    repo, request, _ = market
    previous = date(2026, 11, 4)
    for asset, symbol, day in (("stock", STOCK, DAY), ("etf", ETF, previous)):
        table = "kline_etf_minute" if asset == "etf" else "kline_minute"
        kline_sync._write_minute_partition(_bars(symbol, day), repo.store.data_dir / table)
        repo.refresh_view(table, f"{repo.store.data_dir}/{table}/**/*.parquet")
    windows = []

    def capture(symbols, **kwargs):
        windows.append(kwargs)
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_minute_batch", capture)
    kline_sync.sync_and_persist_minute(
        [ETF], repo, request.app.state.capabilities, asset_type="etf", extend_backward=extend_backward,
    )
    boundary = windows[0]["end_time" if extend_backward else "start_time"]
    assert boundary.date() == previous
    assert boundary.minute == (31 if extend_backward else 32)
