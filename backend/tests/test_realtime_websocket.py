from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import realtime
from app.services.realtime_market_data import RealtimeHub


class FakeRedis:
    def __init__(self) -> None:
        self.values = {}

    async def mget(self, keys):
        return [self.values.get(key) for key in keys]

    def pubsub(self):
        return self

    async def subscribe(self, _channel):
        return None

    async def get_message(self, **_kwargs):
        return None

    async def aclose(self):
        return None


def client(*, heartbeat_seconds: float = 15.0) -> tuple[TestClient, RealtimeHub]:
    app = FastAPI()
    hub = RealtimeHub(
        "redis://test",
        heartbeat_seconds=heartbeat_seconds,
        allowed_origins={"http://allowed.test"},
        redis_factory=lambda _url: FakeRedis(),
    )
    app.state.realtime_hub = hub
    app.include_router(realtime.router)
    return TestClient(app), hub


def test_allowed_origin_receives_hello_and_disconnect_cleans_up() -> None:
    test_client, hub = client()

    with test_client.websocket_connect(
        "/ws/realtime",
        headers={"origin": "http://allowed.test"},
    ) as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
        assert hello["version"] == "v1"
        assert hello["heartbeatSeconds"] == 15
        assert hub.metrics()["active_clients"] == 1

    assert hub.metrics()["active_clients"] == 0
    assert hub.subscriptions == {}


def test_disallowed_origin_is_closed_with_policy_violation() -> None:
    test_client, _hub = client()

    try:
        with test_client.websocket_connect(
            "/ws/realtime",
            headers={"origin": "http://evil.test"},
        ):
            raise AssertionError("connection should not be accepted")
    except Exception as exc:
        assert getattr(exc, "code", None) == 1008


def test_subscribe_hydrates_snapshot_and_unsubscribe_updates_index() -> None:
    test_client, hub = client()
    payload = {
        "type": "update",
        "version": "v1",
        "streamId": "s1",
        "sequence": 1,
        "symbol": "700.HK",
        "market": "hk",
        "eventAt": "2026-07-24T10:00:00+08:00",
        "publishedAt": "2026-07-24T10:00:00.100+08:00",
        "datasets": {
            "quote": {
                "lastDone": 550,
                "prevClose": 548,
                "open": 549,
                "high": 552,
                "low": 547,
                "volume": 100,
                "turnover": 1000,
                "tradeStatus": "Normal",
                "timestamp": "2026-07-24T10:00:00+08:00",
            }
        },
    }
    hub._redis.values = {"lb:ui:v1:latest:700.HK": __import__("json").dumps(payload)}

    with test_client.websocket_connect(
        "/ws/realtime",
        headers={"origin": "http://allowed.test"},
    ) as websocket:
        websocket.receive_json()
        websocket.send_json(
            {
                "action": "subscribe",
                "symbols": ["700.HK"],
                "datasets": ["quote"],
                "depthLevels": 1,
            }
        )
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["symbol"] == "700.HK"
        assert "700.HK" in hub.subscriptions

        websocket.send_json({"action": "unsubscribe", "symbols": ["700.HK"]})
        acknowledgement = websocket.receive_json()
        assert acknowledgement["type"] == "unsubscribed"
        assert "700.HK" not in hub.subscriptions


def test_subscription_validation_rejects_bad_inputs_and_accepts_500() -> None:
    test_client, _hub = client()
    valid_symbols = [f"{index:05d}.HK" for index in range(500)]

    with test_client.websocket_connect(
        "/ws/realtime",
        headers={"origin": "http://allowed.test"},
    ) as websocket:
        websocket.receive_json()
        websocket.send_json(
            {
                "action": "subscribe",
                "symbols": valid_symbols,
                "datasets": ["quote"],
                "depthLevels": 1,
            }
        )
        assert websocket.receive_json()["type"] == "subscribed"

        for request in (
            {
                "action": "subscribe",
                "symbols": [*valid_symbols, "AAPL.US"],
                "datasets": ["quote"],
                "depthLevels": 1,
            },
            {
                "action": "subscribe",
                "symbols": ["bad"],
                "datasets": ["quote"],
                "depthLevels": 1,
            },
            {
                "action": "subscribe",
                "symbols": ["700.HK"],
                "datasets": ["trades"],
                "depthLevels": 1,
            },
            {
                "action": "subscribe",
                "symbols": ["700.HK"],
                "datasets": ["depth"],
                "depthLevels": 11,
            },
        ):
            websocket.send_json(request)
            assert websocket.receive_json()["type"] == "error"


def test_heartbeat_is_emitted_at_configured_interval() -> None:
    test_client, _hub = client(heartbeat_seconds=0.01)

    with test_client.websocket_connect(
        "/ws/realtime",
        headers={"origin": "http://allowed.test"},
    ) as websocket:
        websocket.receive_json()
        assert websocket.receive_json()["type"] == "heartbeat"
