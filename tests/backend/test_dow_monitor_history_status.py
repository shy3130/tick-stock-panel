from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dow_monitor
from app.services.dow_monitor_history_status import DowMonitorHistoryStatusReader
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore


NOW = datetime(2026, 7, 31, 2, 30, tzinfo=UTC)


def _write_status(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_status_reader_maps_hk_alias_and_preserves_terminal_state(tmp_path) -> None:
    path = tmp_path / "monitor-history-warmup.json"
    _write_status(
        path,
        {
            "updated_at": NOW.isoformat(),
            "symbols": {
                "1347.HK": {
                    "state": "completed",
                    "progress": 100,
                    "missing_timeframes": [],
                    "last_error": None,
                    "updated_at": NOW.isoformat(),
                }
            },
        },
    )

    result = DowMonitorHistoryStatusReader(
        path,
        now_fn=lambda: NOW,
    ).for_symbols(["01347.HK"])

    assert result["01347.HK"].status == "completed"
    assert result["01347.HK"].progress == 100
    assert result["01347.HK"].updated_at == NOW


def test_status_reader_never_raises_for_missing_malformed_or_stale_data(tmp_path) -> None:
    path = tmp_path / "monitor-history-warmup.json"
    reader = DowMonitorHistoryStatusReader(path, now_fn=lambda: NOW)
    assert reader.for_symbols(["RNG.US"])["RNG.US"].status == "pending"

    path.write_text("{not-json", encoding="utf-8")
    malformed = reader.for_symbols(["RNG.US"])["RNG.US"]
    assert malformed.status == "unknown"
    assert malformed.last_error == "STATUS_UNAVAILABLE"

    _write_status(
        path,
        {
            "updated_at": (NOW - timedelta(minutes=20)).isoformat(),
            "symbols": {
                "RNG.US": {
                    "state": "running",
                    "progress": 40,
                    "missing_timeframes": ["15m", "day"],
                    "last_error": None,
                    "updated_at": (NOW - timedelta(minutes=20)).isoformat(),
                }
            },
        },
    )
    stale = reader.for_symbols(["RNG.US"])["RNG.US"]
    assert stale.status == "unknown"
    assert stale.progress == 40
    assert stale.last_error == "STATUS_STALE"


class _TrackingStatusReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def for_symbols(self, symbols):
        self.calls.append(tuple(symbols))
        return {}


class _ExplodesOnAccess:
    def __getattr__(self, name):
        raise AssertionError(f"market gateway must not be accessed: {name}")


def _service(tmp_path, *, reader=None) -> DowMonitorService:
    return DowMonitorService(
        DowMonitorStore(tmp_path),
        _ExplodesOnAccess(),
        object(),
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("daily history must not be loaded")
        ),
        now_fn=lambda: NOW,
        history_status_reader=reader,
    )


def _client(service: DowMonitorService) -> TestClient:
    app = FastAPI()
    app.state.dow_monitor_service = service
    app.include_router(dow_monitor.router)
    return TestClient(app)


def test_overview_reads_history_status_once_for_all_symbols(tmp_path) -> None:
    reader = _TrackingStatusReader()
    service = _service(tmp_path, reader=reader)
    service.store.upsert_symbol("RNG.US", "us", True)
    service.store.upsert_symbol("01347.HK", "hk", True)

    response = _client(service).get("/api/dow-monitor/overview")

    assert response.status_code == 200
    assert reader.calls == [("RNG.US", "01347.HK")]
    assert all("history_backfill" in item for item in response.json()["symbols"])


def test_add_symbol_returns_without_history_or_gateway_io(tmp_path) -> None:
    response = _client(_service(tmp_path)).post(
        "/api/dow-monitor/symbols",
        json={"symbol": "RNG.US", "enabled": True},
    )

    assert response.status_code == 200
    assert response.json()["symbol"] == "RNG.US"
