from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.realtime_market_data import RealtimeConnection, UI_DATASETS


router = APIRouter()
# Hong Kong storage and UI aliases use one to five digits in existing
# production data (for example 700.HK, 0981.HK, and 01347.HK).
SYMBOL_RE = re.compile(
    r"^(?:[0-9]{1,5}\.HK|[A-Z][A-Z0-9.-]{0,15}\.US|[0-9]{6}\.(?:SH|SZ|BJ))$"
)


def _error(detail: str) -> dict[str, str]:
    return {"type": "error", "version": "v1", "detail": detail}


async def _send_loop(connection: RealtimeConnection) -> None:
    while True:
        message = await connection.buffer.get()
        await connection.websocket.send_json(message)


async def _heartbeat_loop(connection: RealtimeConnection, seconds: float) -> None:
    while True:
        await asyncio.sleep(seconds)
        await connection.buffer.put(
            "__heartbeat__",
            {
                "type": "heartbeat",
                "version": "v1",
                "serverTime": datetime.now(UTC).isoformat(),
            },
        )


@router.websocket("/ws/realtime")
async def realtime_socket(websocket: WebSocket) -> None:
    hub = websocket.app.state.realtime_hub
    origin = websocket.headers.get("origin", "")
    if origin not in hub.allowed_origins:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    connection = await hub.register(websocket)
    await websocket.send_json(
        {
            "type": "hello",
            "version": "v1",
            "serverTime": datetime.now(UTC).isoformat(),
            "heartbeatSeconds": hub.heartbeat_seconds,
        }
    )
    sender = asyncio.create_task(_send_loop(connection))
    heartbeat = asyncio.create_task(
        _heartbeat_loop(connection, hub.heartbeat_seconds)
    )
    try:
        while True:
            request = await websocket.receive_json()
            if not isinstance(request, dict):
                await connection.buffer.put("__error__", _error("request must be an object"))
                continue
            action = request.get("action")
            raw_symbols = request.get("symbols")
            if action not in {"subscribe", "unsubscribe"}:
                await connection.buffer.put("__error__", _error("unsupported action"))
                continue
            if not isinstance(raw_symbols, list) or not all(
                isinstance(symbol, str) for symbol in raw_symbols
            ):
                await connection.buffer.put("__error__", _error("symbols must be a string list"))
                continue
            symbols = {symbol.strip().upper() for symbol in raw_symbols}
            if not symbols or any(not SYMBOL_RE.fullmatch(symbol) for symbol in symbols):
                await connection.buffer.put("__error__", _error("malformed symbol"))
                continue

            if action == "unsubscribe":
                await hub.unsubscribe(connection, symbols)
                await connection.buffer.put(
                    "__unsubscribed__",
                    {
                        "type": "unsubscribed",
                        "version": "v1",
                        "symbols": sorted(symbols),
                    },
                )
                continue

            combined_symbols = connection.symbols | symbols
            if len(combined_symbols) > 500:
                await connection.buffer.put(
                    "__error__",
                    _error("subscription exceeds 500 symbols"),
                )
                continue
            raw_datasets = request.get("datasets", list(UI_DATASETS))
            if not isinstance(raw_datasets, list):
                await connection.buffer.put("__error__", _error("datasets must be a list"))
                continue
            datasets = {
                str(dataset).strip().lower() for dataset in raw_datasets
            }
            if not datasets or not datasets <= UI_DATASETS:
                await connection.buffer.put("__error__", _error("unsupported dataset"))
                continue
            depth_levels = request.get("depthLevels", 1)
            if (
                not isinstance(depth_levels, int)
                or isinstance(depth_levels, bool)
                or not 1 <= depth_levels <= 10
            ):
                await connection.buffer.put(
                    "__error__",
                    _error("depthLevels must be between 1 and 10"),
                )
                continue
            await hub.subscribe(
                connection,
                symbols,
                datasets,
                depth_levels,
            )
            snapshots = await hub.snapshot(symbols, datasets, depth_levels)
            if snapshots:
                for snapshot in snapshots:
                    await connection.buffer.put(str(snapshot["symbol"]), snapshot)
            else:
                await connection.buffer.put(
                    "__subscribed__",
                    {
                        "type": "subscribed",
                        "version": "v1",
                        "symbols": sorted(symbols),
                    },
                )
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        sender.cancel()
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await hub.unregister(connection)
