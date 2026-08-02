from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dow_monitor
from app.services.dow_monitor_models import DowNotification, DowTimeframeState
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore

NOW = datetime(2026, 8, 2, 15, 0, tzinfo=UTC)
TIMEFRAMES = ("5m", "15m", "30m", "60m", "day")


class _Unused:
    pass


def _service(tmp_path) -> DowMonitorService:
    store = DowMonitorStore(tmp_path)
    store.upsert_symbol("NBIS.US", "us", True)
    return DowMonitorService(
        store,
        _Unused(),
        _Unused(),
        lambda *_args: None,
        now_fn=lambda: NOW,
    )


def _client(service: DowMonitorService) -> TestClient:
    app = FastAPI()
    app.state.dow_monitor_service = service
    app.include_router(dow_monitor.router)
    return TestClient(app)


def _bars(count: int, *, previous_day: int = 0) -> list[dict]:
    start = datetime(2026, 8, 1, 13, 30, tzinfo=UTC)
    rows = []
    for index in range(count):
        timestamp = start + timedelta(minutes=index * 5)
        if index >= previous_day:
            timestamp += timedelta(days=1)
        rows.append(
            {
                "index": index,
                "timestamp": timestamp.isoformat(),
                "open": 10 + index,
                "high": 11 + index,
                "low": 9 + index,
                "close": 10.5 + index,
                "volume": 1_000 + index,
                "ma5": 10.2 + index,
                "ma10": 10.1 + index,
                "ma20": 10.0 + index,
                "detailOnly": {"payload": "x" * 1_000},
            }
        )
    return rows


def _save_states(service: DowMonitorService) -> None:
    for timeframe in TIMEFRAMES:
        service.store.save_state(
            DowTimeframeState(
                symbol="NBIS.US",
                market="us",
                timeframe=timeframe,
                freshness_state="LIVE",
                source_timestamp=NOW,
                snapshot={
                    "bar_time": NOW.isoformat(),
                    "bar_completion": "FINAL",
                    "provisional": False,
                    "volume_ratio_20": 1.5,
                },
                chart={
                    "bars": _bars(20, previous_day=2),
                    "turning": {
                        "signals": [],
                        "pivots": [{"large": "detail-only"}],
                        "lines": [{"large": "detail-only"}],
                        "openingBoxes": [{"large": "detail-only"}],
                    },
                    "lines": [{"large": "detail-only"}],
                    "signals": [{"large": "detail-only"}],
                    "longTerm": {"large": "detail-only"},
                    "headShoulders": {"large": "detail-only"},
                },
                updated_at=NOW,
            )
        )


def _notification(index: int = 1) -> DowNotification:
    return DowNotification(
        notification_id=f"notification-{index}",
        event_key=f"event-{index}",
        symbol="NBIS.US",
        market="us",
        timeframe="15m",
        side="BUY",
        action_name="buy",
        shape_name="breakout",
        triggered_at=NOW + timedelta(seconds=index),
        trigger_price=38.5,
        snapshot_payload={"engine": {"bars": ["x" * 100_000]}},
    )


def test_list_overview_reads_states_once_and_returns_compact_projection(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    _save_states(service)
    original_list_states = service.store.list_states
    calls = 0

    def counted_list_states():
        nonlocal calls
        calls += 1
        return original_list_states()

    monkeypatch.setattr(service.store, "list_states", counted_list_states)
    monkeypatch.setattr(
        service.store,
        "get_state",
        lambda *_args: (_ for _ in ()).throw(AssertionError("get_state must not be used")),
    )

    response = _client(service).get("/api/dow-monitor/list-overview?market=us")

    assert response.status_code == 200
    assert calls == 1
    symbol = response.json()["symbols"][0]
    states = symbol["states"]
    assert len(states["5m"]["chart"]["bars"]) == 18
    assert len(states["15m"]["chart"]["bars"]) == 16
    assert len(states["30m"]["chart"]["bars"]) == 2
    assert states["60m"]["chart"].get("bars", []) == []
    assert states["day"]["chart"].get("bars", []) == []
    for state in states.values():
        assert set(state["chart"]) <= {"bars", "turning"}
        assert set(state["chart"].get("turning", {})) <= {"signals"}
        for bar in state["chart"].get("bars", []):
            assert set(bar) <= {
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "ma5",
                "ma10",
                "ma20",
            }


def test_legacy_overview_also_uses_one_bulk_state_read(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    _save_states(service)
    original_list_states = service.store.list_states
    calls = 0

    def counted_list_states():
        nonlocal calls
        calls += 1
        return original_list_states()

    monkeypatch.setattr(service.store, "list_states", counted_list_states)
    monkeypatch.setattr(
        service.store,
        "get_state",
        lambda *_args: (_ for _ in ()).throw(AssertionError("get_state must not be used")),
    )

    response = _client(service).get("/api/dow-monitor/overview?market=us")

    assert response.status_code == 200
    assert calls == 1


def test_notification_summary_excludes_large_payload_and_reuses_unchanged_file(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    assert service.store.append_notification(_notification())
    original_load = service.store._load_notifications
    calls = 0

    def counted_load():
        nonlocal calls
        calls += 1
        return original_load()

    monkeypatch.setattr(service.store, "_load_notifications", counted_load)
    client = _client(service)

    first = client.get("/api/dow-monitor/notification-summaries?market=us")
    second = client.get("/api/dow-monitor/notification-summaries?market=us")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 0
    item = first.json()["notifications"][0]
    assert "snapshot_payload" not in item
    assert "prompt_text" not in item
    assert "evidence_text" not in item
    assert item["symbol"] == "NBIS.US"
    assert item["side"] == "BUY"


def test_compact_payload_stays_within_approved_budgets(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.remove_symbol("NBIS.US")
    for index in range(20):
        symbol = f"FAST{index}.US"
        service.store.upsert_symbol(symbol, "us", True)
        for timeframe in TIMEFRAMES:
            service.store.save_state(
                DowTimeframeState(
                    symbol=symbol,
                    market="us",
                    timeframe=timeframe,
                    freshness_state="LIVE",
                    source_timestamp=NOW,
                    snapshot={"bar_completion": "FINAL", "provisional": False},
                    chart={"bars": _bars(80), "turning": {"signals": []}},
                    updated_at=NOW,
                )
            )
    for index in range(100):
        service.store.append_notification(_notification(index))
    client = _client(service)

    overview = client.get("/api/dow-monitor/list-overview?market=us")
    notifications = client.get("/api/dow-monitor/notification-summaries?market=us")

    assert len(overview.content) <= 1_000_000
    assert len(notifications.content) <= 256_000
    assert len(json.loads(overview.content)["symbols"]) == 20
    assert len(json.loads(notifications.content)["notifications"]) == 100
