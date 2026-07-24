from __future__ import annotations

import json

import pytest
from redis.exceptions import ConnectionError

from app.services.realtime_market_data import (
    LatestOutboundBuffer,
    RealtimeConnection,
    RealtimeHub,
)


def update(symbol: str, sequence: int = 1) -> dict:
    return {
        "type": "update",
        "version": "v1",
        "streamId": "stream-1",
        "sequence": sequence,
        "symbol": symbol,
        "market": "hk" if symbol.endswith(".HK") else "us",
        "eventAt": "2026-07-24T10:00:00+08:00",
        "publishedAt": "2026-07-24T10:00:00.100+08:00",
        "datasets": {
            "quote": {
                "lastDone": 550 + sequence,
                "prevClose": 548,
                "open": 549,
                "high": 552,
                "low": 547,
                "volume": 100,
                "turnover": 1000,
                "tradeStatus": "Normal",
                "timestamp": "2026-07-24T10:00:00+08:00",
            },
            "depth": {
                "bids": [
                    {"position": index, "price": 550 - index, "volume": 100, "orderCount": 1}
                    for index in range(1, 11)
                ],
                "asks": [
                    {"position": index, "price": 550 + index, "volume": 100, "orderCount": 1}
                    for index in range(1, 11)
                ],
                "timestamp": "2026-07-24T10:00:00+08:00",
            },
        },
    }


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.messages: list[dict] = []
        self.raise_connection_error = False
        self.closed = False

    async def mget(self, keys: list[str]):
        if self.raise_connection_error:
            raise ConnectionError("offline")
        return [self.values.get(key) for key in keys]

    def pubsub(self):
        return self

    async def subscribe(self, _channel: str) -> None:
        if self.raise_connection_error:
            raise ConnectionError("offline")

    async def get_message(self, **_kwargs):
        if self.raise_connection_error:
            raise ConnectionError("offline")
        return self.messages.pop(0) if self.messages else None

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_latest_outbound_buffer_replaces_obsolete_symbol_state() -> None:
    buffer = LatestOutboundBuffer(capacity=2)

    await buffer.put("700.HK", update("700.HK", 1))
    await buffer.put("700.HK", update("700.HK", 2))

    assert (await buffer.get())["sequence"] == 2
    assert buffer.superseded == 1


@pytest.mark.asyncio
async def test_hub_filters_by_symbol_and_projects_requested_depth() -> None:
    hub = RealtimeHub("redis://test", redis_factory=lambda _url: FakeRedis())
    client_700 = RealtimeConnection(depth_levels=1)
    client_aapl = RealtimeConnection(depth_levels=10)
    await hub.register(client_700)
    await hub.register(client_aapl)
    await hub.subscribe(client_700, {"700.HK"}, {"quote", "depth"})
    await hub.subscribe(client_aapl, {"AAPL.US"}, {"quote", "depth"})

    await hub.dispatch(update("700.HK"))

    projected = await client_700.buffer.get()
    assert len(projected["datasets"]["depth"]["bids"]) == 1
    assert client_aapl.buffer.pending_symbols == set()
    assert hub.metrics()["sent_updates"] == 1


@pytest.mark.asyncio
async def test_snapshot_mget_sanitizes_unknown_fields_and_projects_depth() -> None:
    redis = FakeRedis()
    payload = update("700.HK")
    payload["account"] = {"cash": 1}
    redis.values["lb:ui:v1:latest:700.HK"] = json.dumps(payload)
    hub = RealtimeHub("redis://test", redis_factory=lambda _url: redis)

    snapshots = await hub.snapshot({"700.HK"}, {"depth"}, depth_levels=1)

    assert len(snapshots) == 1
    assert snapshots[0]["type"] == "snapshot"
    assert "account" not in snapshots[0]
    assert set(snapshots[0]["datasets"]) == {"depth"}
    assert len(snapshots[0]["datasets"]["depth"]["asks"]) == 1


@pytest.mark.asyncio
async def test_unsubscribe_and_unregister_remove_symbol_indexes() -> None:
    hub = RealtimeHub("redis://test", redis_factory=lambda _url: FakeRedis())
    connection = RealtimeConnection()
    await hub.register(connection)
    await hub.subscribe(connection, {"700.HK", "AAPL.US"}, {"quote"})

    await hub.unsubscribe(connection, {"700.HK"})
    assert "700.HK" not in hub.subscriptions
    assert hub.subscriptions["AAPL.US"] == {connection}

    await hub.unregister(connection)
    assert hub.subscriptions == {}
    assert connection.buffer.closed


@pytest.mark.asyncio
async def test_pubsub_failure_degrades_and_sends_fallback_without_raising() -> None:
    redis = FakeRedis()
    hub = RealtimeHub("redis://test", redis_factory=lambda _url: redis)
    connection = RealtimeConnection()
    await hub.register(connection)
    redis.raise_connection_error = True

    await hub.run_pubsub_once()

    assert hub.state == "degraded"
    assert (await connection.buffer.get())["type"] == "fallback"
    assert hub.metrics()["redis_failures"] == 1
