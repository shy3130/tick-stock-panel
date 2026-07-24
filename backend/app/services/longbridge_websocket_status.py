"""Read the runtime snapshot emitted by the Longbridge WebSocket collector."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATUS_PATH = Path("/run/longbridge/quote-subscription.json")


def _status_path() -> Path:
    value = os.getenv("LONGBRIDGE_WS_STATUS_FILE", "").strip()
    return Path(value) if value else DEFAULT_STATUS_PATH


def _missing_status(message: str = "未读取到长桥 WebSocket 运行状态") -> dict[str, Any]:
    return {
        "provider": "longbridge",
        "transport": "websocket",
        "configured": False,
        "running": False,
        "healthy": False,
        "status": "unavailable",
        "heartbeat_at": None,
        "subscription": {
            "source": "",
            "limit": 0,
            "markets": [],
            "reload_seconds": 0,
            "data_types": [],
            "priority": [],
            "sinks": [],
            "buffer_capacity": 0,
            "flush_batch_size": 0,
            "flush_interval_ms": 0,
        },
        "activity": {
            "available": False,
            "symbol_count": 0,
            "markets": {"cn": 0, "hk": 0, "us": 0},
            "last_event_at": None,
        },
        "error": message,
    }


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_longbridge_websocket_status(path: Path | None = None) -> dict[str, Any]:
    """Return collector-owned configuration and health without querying market tables."""

    target = path or _status_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _missing_status()
    if not isinstance(payload, dict):
        return _missing_status("长桥 WebSocket 运行状态格式无效")

    heartbeat_at = _parse_time(payload.get("heartbeat_at"))
    try:
        stale_after_seconds = max(30, int(os.getenv("LONGBRIDGE_WS_STALE_AFTER_SECONDS", "120")))
    except ValueError:
        stale_after_seconds = 120
    age_seconds = (
        (datetime.now(timezone.utc) - heartbeat_at).total_seconds()
        if heartbeat_at is not None
        else float("inf")
    )
    stale = age_seconds > stale_after_seconds

    result = dict(payload)
    result["provider"] = "longbridge"
    result["transport"] = "websocket"
    result["configured"] = bool(payload.get("configured"))
    result["running"] = bool(payload.get("running")) and not stale
    result["healthy"] = bool(payload.get("healthy")) and result["running"]
    if stale:
        result["status"] = "stale"
        result["error"] = "长桥 WebSocket 心跳已超时"
    return result
