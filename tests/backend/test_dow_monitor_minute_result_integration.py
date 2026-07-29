from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import polars as pl
from fastapi import FastAPI

from app.services.dow_monitor_data import SymbolFreshness, WebStockBatch
from app.services.dow_monitor_minute_result_materializer import MaterializerStatus
from app.services.dow_monitor_models import DowTimeframeState
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore


NOW = datetime(2026, 7, 29, 2, 31, 10, tzinfo=UTC)


def _save_states(store: DowMonitorStore, symbol: str) -> None:
    for timeframe in ("5m", "15m", "30m", "60m", "day"):
        store.save_state(
            DowTimeframeState(
                symbol=symbol,
                market="hk",
                timeframe=timeframe,
                freshness_state="LIVE",
                source_timestamp=NOW,
                snapshot={},
                chart={},
                updated_at=NOW,
            )
        )


class Gateway:
    def fetch_since(self, _starts, _end) -> WebStockBatch:
        return WebStockBatch(
            quotes=[],
            minute_rows=pl.DataFrame(),
            source_timestamp=NOW,
            freshness_by_symbol={
                "700.HK": SymbolFreshness(state="LIVE", reason=None),
            },
            gap_details={"700.HK": []},
        )


class Materializer:
    def __init__(self, events: list[str], error: str | None = None) -> None:
        self.events = events
        self._status = MaterializerStatus(last_error=error)

    def materialize(self, symbols, now):
        assert [item.symbol for item in symbols] == ["700.HK"]
        assert now == NOW
        self.events.append("minute-results")

    def status(self) -> MaterializerStatus:
        return self._status


def _service(tmp_path, monkeypatch, materializer: Materializer, events: list[str]):
    store = DowMonitorStore(tmp_path)
    store.upsert_symbol("700.HK", "hk", True)
    _save_states(store, "700.HK")
    service = DowMonitorService(
        store,
        Gateway(),
        object(),
        lambda *_args: pl.DataFrame(),
        now_fn=lambda: NOW,
        minute_result_materializer=materializer,
    )
    monkeypatch.setattr(
        service,
        "_evaluate_symbol",
        lambda *_args, **_kwargs: events.append("formal-signals") or (None, True),
    )
    monkeypatch.setattr(
        service,
        "_refresh_minute_decision",
        lambda *_args, **_kwargs: events.append("minute-decision"),
    )
    monkeypatch.setattr(service, "_intraday_capital_by_symbol", lambda _symbols: {})
    return service


def test_monitor_cycle_materializes_after_signal_and_minute_decision(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    service = _service(tmp_path, monkeypatch, Materializer(events), events)

    asyncio.run(service.run_once())

    assert events == ["formal-signals", "minute-decision", "minute-results"]


def test_materializer_error_does_not_change_monitor_success_state(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    service = _service(
        tmp_path,
        monkeypatch,
        Materializer(events, error="clickhouse unavailable"),
        events,
    )

    asyncio.run(service.run_once())
    status = service.status()

    assert status["last_success_at"] == NOW.isoformat()
    assert status["last_error"] is None
    assert status["minute_results"]["last_error"] == "clickhouse unavailable"


def test_schema_failure_keeps_monitor_running_with_disabled_materializer(
    monkeypatch,
    tmp_path,
) -> None:
    from app import main

    captured: dict[str, object] = {}

    class Store:
        def __init__(self, _data_dir) -> None:
            pass

        def list_notifications(self, limit=1000):
            return []

    class Repository:
        def ensure_schema(self) -> None:
            raise RuntimeError("clickhouse unavailable")

    class Service:
        def __init__(self, *_args, minute_result_materializer=None, **_kwargs) -> None:
            captured["materializer"] = minute_result_materializer

        async def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(main, "DowMonitorStore", Store)
    monkeypatch.setattr(main, "DowMonitorService", Service)
    monkeypatch.setattr(main, "LongbridgeDowClient", lambda _endpoint: object())
    monkeypatch.setattr(main, "WebStockMonitorGateway", lambda _provider: object())
    monkeypatch.setattr(
        main,
        "DowMonitorMinuteResultRepository",
        lambda: Repository(),
    )

    app = FastAPI()
    asyncio.run(main._start_dow_monitor(app, tmp_path, object(), "http://dow"))

    assert captured["started"] is True
    status = captured["materializer"].status()
    assert status.enabled is False
    assert status.last_error == "clickhouse unavailable"
