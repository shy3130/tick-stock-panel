from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api import dow_monitor
from app.services.dow_monitor_models import DowNotification, DowTimeframeState
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


class _UnusedGateway:
    pass


class _UnusedDowClient:
    pass


def _daily_loader(*_args):
    raise AssertionError("API handlers must not load market data")


def _service(tmp_path) -> DowMonitorService:
    return DowMonitorService(
        DowMonitorStore(tmp_path),
        _UnusedGateway(),
        _UnusedDowClient(),
        _daily_loader,
        now_fn=lambda: NOW,
    )


def _client(service: DowMonitorService) -> TestClient:
    app = FastAPI()
    app.state.dow_monitor_service = service
    app.include_router(dow_monitor.router)
    return TestClient(app)


def test_symbols_are_normalized_and_duplicate_add_is_idempotent(tmp_path) -> None:
    service = _service(tmp_path)
    client = _client(service)

    first = client.post("/api/dow-monitor/symbols", json={"symbol": " 01347.hk "})
    second = client.post("/api/dow-monitor/symbols", json={"symbol": "01347.HK"})

    assert first.status_code == 200
    assert first.json()["symbol"] == "01347.HK"
    assert second.status_code == 200
    assert len(client.get("/api/dow-monitor/symbols").json()["symbols"]) == 1


def test_zero_padded_hk_alias_cannot_create_a_duplicate_monitor(tmp_path) -> None:
    service = _service(tmp_path)
    client = _client(service)

    first = client.post("/api/dow-monitor/symbols", json={"symbol": "02714.HK"})
    second = client.post("/api/dow-monitor/symbols", json={"symbol": "2714.HK"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["symbol"] == "02714.HK"
    assert [item["symbol"] for item in client.get("/api/dow-monitor/symbols").json()["symbols"]] == [
        "02714.HK"
    ]


def test_symbols_reject_unsupported_suffix(tmp_path) -> None:
    client = _client(_service(tmp_path))

    response = client.post("/api/dow-monitor/symbols", json={"symbol": "BTC.CRYPTO"})

    assert response.status_code == 400


def test_patch_switch_persists_normalized_symbol(tmp_path) -> None:
    service = _service(tmp_path)
    client = _client(service)
    client.post("/api/dow-monitor/symbols", json={"symbol": "01347.HK"})

    response = client.patch("/api/dow-monitor/symbols/ 01347.hk ", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert service.store.list_symbols()[0].enabled is False


def test_market_filter_changes_response_only_not_enabled_state(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service.store.upsert_symbol("INTC.US", "us", True)
    client = _client(service)

    response = client.get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    assert {item["market"] for item in response.json()["symbols"]} == {"hk"}
    assert next(item for item in service.store.list_symbols() if item.symbol == "INTC.US").enabled


def test_overview_api_exposes_authoritative_quote_header_fields(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service._latest_quotes_by_symbol["01347.HK"] = {
        "symbol": "01347.HK",
        "name": "华丰科技",
        "last_price": 13.47,
        "change_pct": 0.0125,
        "timestamp": int(NOW.timestamp() * 1_000),
    }

    response = _client(service).get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    expected = {
        "name": "华丰科技",
        "last_price": 13.47,
        "change_pct": 0.0125,
        "quote_timestamp": int(NOW.timestamp() * 1_000),
    }
    item = response.json()["symbols"][0]
    assert {key: item[key] for key in expected} == expected


def test_detail_validates_timeframe_and_preserves_long_term_sidecar(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service.store.save_state(
        DowTimeframeState(
            symbol="01347.HK",
            market="hk",
            timeframe="5m",
            freshness_state="LIVE",
            source_timestamp=NOW,
            snapshot={},
            chart={"longTerm": {"trendDirection": "UP", "operation": "持有"}},
            updated_at=NOW,
        )
    )
    client = _client(service)

    valid = client.get("/api/dow-monitor/01347.hk?timeframe=5m")
    invalid = client.get("/api/dow-monitor/01347.HK?timeframe=1m")

    assert valid.status_code == 200
    assert valid.json()["chart"]["longTerm"] == {
        "trendDirection": "UP",
        "operation": "持有",
    }
    assert invalid.status_code == 422


def test_notifications_read_and_status_expose_persisted_timestamps(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service.store.upsert_symbol("INTC.US", "us", False)
    service.store.append_notification(
        DowNotification(
            notification_id="notification-1",
            event_key="event-1",
            symbol="01347.HK",
            market="hk",
            timeframe="5m",
            side="BUY",
            action_name="buy",
            shape_name="shape",
            triggered_at=NOW,
            trigger_price=12.3,
            snapshot_payload={"engine": {"longTerm": {"trendDirection": "UP"}}},
        )
    )
    client = _client(service)

    notifications = client.get("/api/dow-monitor/notifications?market=hk&unreadOnly=true")
    read = client.patch("/api/dow-monitor/notifications/notification-1/read")
    status = client.get("/api/dow-monitor/status")

    assert notifications.status_code == 200
    assert notifications.json()["notifications"][0]["triggered_at"] == "2026-07-23T08:00:00Z"
    assert read.status_code == 200
    assert read.json()["read_at"] is not None
    assert (
        client.get("/api/dow-monitor/notifications?unreadOnly=true").json()["notifications"] == []
    )
    assert status.status_code == 200
    assert set(status.json()) >= {
        "last_started_at",
        "last_completed_at",
        "last_success_at",
    }
    assert status.json()["enabled_markets"] == ["hk"]
    assert status.json()["open_enabled_markets"] == []


def test_health_status_is_available_only_to_loopback_patrol_without_browser_session(
    monkeypatch,
) -> None:
    from app import main
    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: True)

    async def call_next(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def request(peer: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/dow-monitor/status",
                "raw_path": b"/api/dow-monitor/status",
                "query_string": b"",
                "headers": headers or [],
                "client": (peer, 50_000),
                "server": ("tickflow", 3018),
            }
        )

    ipv4 = asyncio.run(main.auth_middleware(request("127.0.0.1"), call_next))
    ipv6 = asyncio.run(main.auth_middleware(request("::1"), call_next))
    remote = asyncio.run(main.auth_middleware(request("192.168.10.99"), call_next))
    spoofed = asyncio.run(
        main.auth_middleware(
            request("192.168.10.99", [(b"x-forwarded-for", b"127.0.0.1")]),
            call_next,
        )
    )

    assert ipv4.status_code == 200
    assert ipv6.status_code == 200
    assert remote.status_code == 401
    assert spoofed.status_code == 401


def test_monitor_symbol_feed_is_get_only_and_loopback_only_without_browser_session(
    monkeypatch,
) -> None:
    from app import main
    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: True)

    async def call_next(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def request(peer: str, method: str) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": "/api/dow-monitor/symbols",
                "raw_path": b"/api/dow-monitor/symbols",
                "query_string": b"",
                "headers": [],
                "client": (peer, 50_000),
                "server": ("tickflow", 3018),
            }
        )

    loopback_get = asyncio.run(main.auth_middleware(request("127.0.0.1", "GET"), call_next))
    loopback_post = asyncio.run(main.auth_middleware(request("127.0.0.1", "POST"), call_next))
    remote_get = asyncio.run(main.auth_middleware(request("192.168.10.99", "GET"), call_next))

    assert loopback_get.status_code == 200
    assert loopback_post.status_code == 401
    assert remote_get.status_code == 401


def test_notification_read_returns_exact_oldest_notification_beyond_list_limit(tmp_path) -> None:
    service = _service(tmp_path)
    first = DowNotification(
        notification_id="first-notification",
        event_key="first-event",
        symbol="01347.HK",
        market="hk",
        timeframe="5m",
        side="BUY",
        action_name="buy",
        shape_name="shape",
        triggered_at=NOW - timedelta(days=1),
        trigger_price=12.3,
        snapshot_payload={},
    )
    assert service.store.append_notification(first)
    for index in range(1, 1_001):
        assert service.store.append_notification(
            first.model_copy(
                update={
                    "notification_id": f"notification-{index}",
                    "event_key": f"event-{index}",
                    "triggered_at": NOW + timedelta(seconds=index),
                }
            )
        )

    response = _client(service).patch("/api/dow-monitor/notifications/first-notification/read")

    assert response.status_code == 200
    assert response.json()["notification_id"] == "first-notification"
    assert response.json()["read_at"] is not None


def test_uninitialized_service_returns_503() -> None:
    app = FastAPI()
    app.include_router(dow_monitor.router)

    response = TestClient(app).get("/api/dow-monitor/status")

    assert response.status_code == 503


def test_lifecycle_starts_single_monitor_with_registered_clickhouse_provider(
    monkeypatch, tmp_path
) -> None:
    from app import main

    events: list[object] = []

    class FakeStore:
        def __init__(self, data_dir) -> None:
            events.append(("store", data_dir))

    class FakeGateway:
        def __init__(self, provider) -> None:
            events.append(("gateway", provider))

    class FakeClient:
        def __init__(self, endpoint) -> None:
            events.append(("client", endpoint))

    class FakeService:
        def __init__(self, store, gateway, client, daily_loader) -> None:
            self.store = store
            self.gateway = gateway
            self.client = client
            self.daily_loader = daily_loader
            self.started = 0

        async def start(self) -> None:
            self.started += 1
            events.append("start")

    provider = SimpleNamespace(get_daily=lambda *_args: "daily")
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(main, "DowMonitorStore", FakeStore, raising=False)
    monkeypatch.setattr(main, "WebStockMonitorGateway", FakeGateway, raising=False)
    monkeypatch.setattr(main, "LongbridgeDowClient", FakeClient, raising=False)
    monkeypatch.setattr(main, "DowMonitorService", FakeService, raising=False)

    asyncio.run(main._start_dow_monitor(app, tmp_path, provider, "http://engine"))

    assert events == [
        ("store", tmp_path),
        ("gateway", provider),
        ("client", "http://engine"),
        "start",
    ]
    assert app.state.dow_monitor_service.started == 1
    assert app.state.dow_monitor_service.daily_loader("01347.HK", NOW) == "daily"


def test_lifecycle_stops_monitor_before_closing_its_client() -> None:
    from app import main

    events: list[str] = []

    class FakeService:
        async def stop(self) -> None:
            events.append("stop")

    class FakeClient:
        def close(self) -> None:
            events.append("close")

    app = SimpleNamespace(
        state=SimpleNamespace(dow_monitor_service=FakeService(), dow_monitor_client=FakeClient())
    )

    asyncio.run(main._stop_dow_monitor(app))

    assert events == ["stop", "close"]


def test_real_lifespan_loads_provider_before_monitor_and_stops_before_shared_close(
    monkeypatch, tmp_path
) -> None:
    from app import main
    from app.data_providers import custom as custom_sources
    from app.jobs import daily_pipeline
    from app.services import (
        auth,
        ext_presets,
        ext_pull,
        financial_sync,
        preferences,
        wecom_bot_service,
    )
    from app.services import depth_service as depth_service_module
    from app.services import screener as screener_module
    from app.strategy import engine as strategy_engine_module
    from app.strategy import monitor as strategy_monitor_module
    from app.strategy import monitor_rules

    events: list[str] = []
    provider = object()

    class FakeDataStore:
        data_dir = tmp_path

    class FakeRepository:
        enriched_ready = False

        def __init__(self, _store) -> None:
            pass

        def get_matrix_data_generation(self, _asset_type) -> None:
            events.append("matrix")

        def refresh_cache(self, *, background) -> None:
            assert background is True

    class FakeCapabilities:
        def all(self) -> list[object]:
            return []

    class FakeQuoteService:
        def set_repo(self, _repo) -> None:
            pass

        def boot_check(self) -> None:
            pass

        def set_app_state(self, _state) -> None:
            pass

        def stop(self) -> None:
            events.append("shared-close")

    class FakeStrategyMonitor:
        pass

    class FakeDepthService:
        def set_repo(self, _repo) -> None:
            pass

        def set_app_state(self, _state) -> None:
            pass

        def boot_check(self) -> None:
            pass

        def start_polling(self) -> None:
            pass

        def stop_polling(self) -> None:
            pass

    class FakeScheduler:
        def shutdown(self, *, wait) -> None:
            assert wait is False

    class FakePullScheduler:
        def start(self, _data_dir) -> None:
            pass

        def refresh(self, _data_dir) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeFinancialScheduler:
        def start(self, _data_dir, _capset) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeScreener:
        _load_enriched_history = staticmethod(lambda *_args: None)

        def __init__(self, _repo, asset_type="stock") -> None:
            assert asset_type in {"stock", "etf"}

    class FakeStrategyEngine:
        def __init__(self, *, strategy_dirs) -> None:
            assert strategy_dirs

        def list_strategies(self) -> list[dict]:
            return []

    class FakeMonitorEngine:
        rule_count = 0

        def set_strategy_engine(self, _engine) -> None:
            pass

        def set_data_dir(self, _data_dir) -> None:
            pass

        def set_history_loader(self, _loader) -> None:
            pass

        def set_history_loader_etf(self, _loader) -> None:
            pass

        def set_rules(self, _rules) -> None:
            pass

    class FakeMonitorService:
        async def stop(self) -> None:
            events.append("monitor-stop")

    class FakeMonitorClient:
        def close(self) -> None:
            events.append("monitor-client-close")

    async def fake_start(app, _data_dir, resolved_provider, _endpoint) -> None:
        assert resolved_provider is provider
        assert events == ["providers-loaded", "clickhouse-resolved"]
        app.state.dow_monitor_service = FakeMonitorService()
        app.state.dow_monitor_client = FakeMonitorClient()
        events.append("monitor-start")

    async def fake_presets(_data_dir) -> None:
        pass

    monkeypatch.setattr(main, "DataStore", FakeDataStore)
    monkeypatch.setattr(main, "KlineRepository", FakeRepository)
    monkeypatch.setattr(main, "QuoteService", FakeQuoteService)
    monkeypatch.setattr(main, "detect_capabilities", lambda: FakeCapabilities())
    monkeypatch.setattr(main, "_start_dow_monitor", fake_start)
    monkeypatch.setattr(main.settings, "backtest_matrix_disk_cache_enabled", False)
    monkeypatch.setattr(main.settings, "backtest_matrix_cache_prewarm", False)
    monkeypatch.setattr(auth, "bootstrap_from_env", lambda: None)
    monkeypatch.setattr(custom_sources, "load_all", lambda: events.append("providers-loaded"))
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(
        custom_sources,
        "get_provider",
        lambda name: (
            events.append("clickhouse-resolved") or provider if name == "clickhouse" else None
        ),
    )
    monkeypatch.setattr(daily_pipeline, "set_app_state", lambda _state: None)
    monkeypatch.setattr(daily_pipeline, "start_scheduler", lambda *_args: FakeScheduler())
    monkeypatch.setattr(strategy_monitor_module, "StrategyMonitorService", FakeStrategyMonitor)
    monkeypatch.setattr(strategy_monitor_module, "MonitorRuleEngine", FakeMonitorEngine)
    monkeypatch.setattr(depth_service_module, "DepthService", FakeDepthService)
    monkeypatch.setattr(ext_presets, "ensure_builtin_presets", fake_presets)
    monkeypatch.setattr(ext_pull, "pull_scheduler", FakePullScheduler())
    monkeypatch.setattr(financial_sync, "financial_scheduler", FakeFinancialScheduler())
    monkeypatch.setattr(screener_module, "ScreenerService", FakeScreener)
    monkeypatch.setattr(strategy_engine_module, "StrategyEngine", FakeStrategyEngine)
    monkeypatch.setattr(preferences, "get_strategy_monitor_enabled", lambda: False)
    monkeypatch.setattr(monitor_rules, "load_all", lambda _data_dir: [])
    monkeypatch.setattr(
        wecom_bot_service,
        "WecomBotService",
        lambda: SimpleNamespace(
            set_app_state=lambda _state: None,
            boot_check=lambda: None,
            stop=lambda: None,
        ),
    )

    async def exercise() -> None:
        app = FastAPI()
        async with main.lifespan(app):
            assert events == ["providers-loaded", "clickhouse-resolved", "monitor-start"]

    asyncio.run(exercise())

    assert events.index("monitor-stop") < events.index("monitor-client-close")
    assert events.index("monitor-client-close") < events.index("shared-close")
