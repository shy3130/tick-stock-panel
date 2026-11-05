"""同标的补齐与分时读取跨窗口共享执行，更新广播给所有订阅者。"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import kline
from app.services import daily_bootstrap, kline_sync
from app.services.quote_service import QuoteService
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


def test_same_minute_sync_executes_once_and_notifies_both_windows(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    repo.save_etf_instruments(pl.DataFrame({"symbol": ["159558.SZ"]}))
    quotes = QuoteService()
    subscribers = [quotes.subscribe(), quotes.subscribe()]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=repo, capabilities=CapabilitySet(), quote_service=quotes,
    )))
    entered, release = threading.Event(), threading.Event()
    calls = []

    def sync(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return 2

    monkeypatch.setattr(kline, "_minute_allowed", lambda _: True)
    monkeypatch.setattr(kline_sync, "sync_and_persist_minute", sync)
    monkeypatch.setattr("app.jobs.daily_pipeline._refresh_single_view", lambda *a: None)

    async def check():
        first = asyncio.create_task(kline.sync_minute_single(request, {"symbol": "159558.SZ", "days": 10}))
        assert await asyncio.to_thread(entered.wait, 3)
        second = asyncio.create_task(kline.sync_minute_single(request, {"symbol": "159558.SZ", "days": 10}))
        await asyncio.sleep(0.05)
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(check())
    assert [r["rows"] for r in results] == [2, 2]
    assert len(calls) == 1
    assert all("minute" in sub.pop()["data_updated"] for sub in subscribers)


def test_same_live_minute_read_is_shared(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return pl.DataFrame({"symbol": ["159558.SZ"], "close": [1.0]}), False

    monkeypatch.setattr(kline_sync, "_try_custom_minute", fetch)
    capset = CapabilitySet()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(kline_sync.fetch_minute_single, "159558.SZ", date(2026, 11, 5), "etf", capset=capset)
        assert entered.wait(3)
        second = pool.submit(kline_sync.fetch_minute_single, "159558.SZ", date(2026, 11, 5), "etf", capset=capset)
        threading.Event().wait(0.05)
        release.set()
        assert first.result(3).equals(second.result(3))
    assert len(calls) == 1


def test_list_and_detail_share_daily_preparation_and_notify_every_window(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    symbol = "159558.SZ"
    repo.save_etf_instruments(pl.DataFrame({"symbol": [symbol]}))
    capset = CapabilitySet()
    quotes = QuoteService()
    subscribers = [quotes.subscribe(), quotes.subscribe()]
    entered, release = threading.Event(), threading.Event()
    calls = []
    day = date(2026, 11, 5)

    def fetch(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        yield pl.DataFrame({
            "symbol": [symbol], "date": [day], "open": [1.0], "high": [1.0], "low": [1.0],
            "close": [1.0], "volume": [100.0], "amount": [100.0],
        })

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    monkeypatch.setattr(kline_sync, "fetch_adj_factor_single", lambda *a, **kw: pl.DataFrame())
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(daily_bootstrap.prepare, repo, capset, [symbol], day, on_updated=quotes.notify_data_updated)
        assert entered.wait(3)
        second = pool.submit(daily_bootstrap.ensure_history, repo, capset, symbol, "etf", date(2026, 10, 5), day,
                             on_updated=quotes.notify_data_updated)
        threading.Event().wait(0.05)
        release.set()
        assert first.result(3)[0]["status"] == "ready"
        assert second.result(3).height == 1
    assert len(calls) == 1
    assert all("daily" in sub.pop()["data_updated"] for sub in subscribers)
