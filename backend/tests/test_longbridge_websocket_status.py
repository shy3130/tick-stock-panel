from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def _snapshot(*, heartbeat_at: str) -> dict:
    return {
        "provider": "longbridge",
        "transport": "websocket",
        "configured": True,
        "running": True,
        "healthy": True,
        "status": "running",
        "heartbeat_at": heartbeat_at,
        "subscription": {
            "source": "priority",
            "limit": 500,
            "markets": ["cn", "hk", "us"],
            "reload_seconds": 300,
            "data_types": ["quote", "depth", "trades", "brokers", "candlestick_1m"],
            "priority": ["holdings_and_traded", "industry_leaders", "strategy_candidates"],
            "sinks": ["clickhouse", "redis"],
            "buffer_capacity": 200000,
            "flush_batch_size": 5000,
            "flush_interval_ms": 250,
        },
        "activity": {
            "available": True,
            "symbol_count": 367,
            "markets": {"cn": 162, "hk": 49, "us": 156},
            "last_event_at": "2026-07-19T00:02:39+08:00",
        },
        "writer": {"queue_depth": 0, "flush_failures": 0},
    }


def test_status_reads_actual_collector_snapshot(tmp_path):
    from app.services.longbridge_websocket_status import get_longbridge_websocket_status

    path = tmp_path / "quote-subscription.json"
    path.write_text(
        json.dumps(_snapshot(heartbeat_at=datetime.now(timezone.utc).isoformat())),
        encoding="utf-8",
    )

    result = get_longbridge_websocket_status(path)

    assert result["configured"] is True
    assert result["running"] is True
    assert result["subscription"]["limit"] == 500
    assert result["subscription"]["data_types"][-1] == "candlestick_1m"
    assert result["activity"]["symbol_count"] == 367
    assert result["activity"]["markets"] == {"cn": 162, "hk": 49, "us": 156}


def test_status_marks_stale_collector_snapshot_not_running(tmp_path):
    from app.services.longbridge_websocket_status import get_longbridge_websocket_status

    path = tmp_path / "quote-subscription.json"
    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    path.write_text(json.dumps(_snapshot(heartbeat_at=stale.isoformat())), encoding="utf-8")

    result = get_longbridge_websocket_status(path)

    assert result["configured"] is True
    assert result["running"] is False
    assert result["healthy"] is False
    assert result["status"] == "stale"


def test_status_degrades_when_snapshot_is_missing(tmp_path):
    from app.services.longbridge_websocket_status import get_longbridge_websocket_status

    result = get_longbridge_websocket_status(tmp_path / "missing.json")

    assert result["configured"] is False
    assert result["running"] is False
    assert result["activity"]["available"] is False
    assert result["error"] == "未读取到长桥 WebSocket 运行状态"
