from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import settings
from app.services import user_context
from app.services.user_context import UserIdentity
from app.sycee.portfolio import router as portfolio_router
from app.sycee.portfolio_sell_alert import router as sell_alert_router


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def bind_test_user(request: Request, call_next):
        user_id = request.headers.get("x-test-user", "admin")
        token = user_context.set_current_user(UserIdentity(id=user_id, username=user_id))
        try:
            return await call_next(request)
        finally:
            user_context.reset(token)

    app.include_router(portfolio_router)
    app.include_router(sell_alert_router)
    return TestClient(app)


def _trade(
    *,
    symbol: str = "600519.SH",
    side: str = "buy",
    quantity: int = 100,
    trade_date: str = "2026-07-27",
) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "side": side,
        "quantity": quantity,
        "price": 10,
        "fees": 0,
        "trade_date": trade_date,
        "note": "",
    }


def _enable(client: TestClient, *, headers: dict | None = None, strategy_id: str = "trend_breakout"):
    return client.put(
        "/api/sycee/portfolio/sell-alert",
        headers=headers,
        json={
            "enabled": True,
            "strategy_id": strategy_id,
            "webhook_channels": ["feishu", "feishu", "wecom"],
        },
    )


def test_sell_alert_builds_a_sell_only_rule_for_current_positions(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    client.post("/api/sycee/portfolio/trades", json=_trade(symbol="600519.SH"))
    client.post("/api/sycee/portfolio/trades", json=_trade(symbol="000001.SZ"))

    response = _enable(client)

    assert response.status_code == 200
    status = response.json()
    assert status["state"] == "ready"
    assert status["symbols"] == ["000001.SZ", "600519.SH"]
    assert status["config"]["webhook_channels"] == ["feishu", "wecom"]
    assert status["config"]["rule_id"].startswith("sycee_pf_sell_")
    assert status["desired_rule"] == {
        "id": status["config"]["rule_id"],
        "name": "持仓卖出提醒",
        "enabled": True,
        "type": "strategy",
        "asset_type": "stock",
        "scope": "symbols",
        "symbols": ["000001.SZ", "600519.SH"],
        "sector": None,
        "strategy_id": "trend_breakout",
        "direction": "exit",
        "notify_events": ["sell_signal"],
        "conditions": [],
        "logic": "and",
        "cooldown_seconds": 3600,
        "severity": "warn",
        "webhook_channels": ["feishu", "wecom"],
        "message": "",
    }


def test_sell_alert_waits_without_positions_and_keeps_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    client.post("/api/sycee/portfolio/trades", json=_trade())
    enabled = _enable(client).json()

    client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(side="sell", quantity=100, trade_date="2026-07-28"),
    )
    status = client.get("/api/sycee/portfolio/sell-alert").json()

    assert status["state"] == "waiting_for_positions"
    assert status["desired_rule"] is None
    assert status["config"]["enabled"] is True
    assert status["config"]["rule_id"] == enabled["config"]["rule_id"]


def test_sell_alert_configuration_is_isolated_per_user(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    admin_headers = {"x-test-user": "admin"}
    alice_headers = {"x-test-user": "alice"}
    client.post("/api/sycee/portfolio/trades", headers=admin_headers, json=_trade())
    client.post(
        "/api/sycee/portfolio/trades",
        headers=alice_headers,
        json=_trade(symbol="000001.SZ"),
    )

    admin = _enable(client, headers=admin_headers, strategy_id="trend_breakout").json()
    alice = _enable(client, headers=alice_headers, strategy_id="macd_golden").json()

    assert admin["symbols"] == ["600519.SH"]
    assert alice["symbols"] == ["000001.SZ"]
    assert admin["config"]["rule_id"] != alice["config"]["rule_id"]
    assert (tmp_path / "users" / "admin" / "sycee" / "portfolio_sell_alert.json").exists()
    assert (tmp_path / "users" / "alice" / "sycee" / "portfolio_sell_alert.json").exists()


def test_sell_alert_rejects_missing_strategy_and_unknown_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    missing_strategy = client.put(
        "/api/sycee/portfolio/sell-alert",
        json={"enabled": True, "strategy_id": "", "webhook_channels": []},
    )
    invalid_channel = client.put(
        "/api/sycee/portfolio/sell-alert",
        json={"enabled": False, "strategy_id": "trend_breakout", "webhook_channels": ["email"]},
    )

    assert missing_strategy.status_code == 422
    assert invalid_channel.status_code == 422


def test_desired_rule_round_trips_through_public_monitor_api_without_touching_user_rules(
    monkeypatch,
    tmp_path,
):
    from app.api.monitor_rules import router as monitor_rules_router

    class StrategyEngine:
        @staticmethod
        def get(strategy_id):
            return {"id": strategy_id}

        @staticmethod
        def validate_context(strategy, context):
            return None

    class MonitorEngine:
        def __init__(self):
            self.rules = []

        def set_rules(self, rules):
            self.rules = rules

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()

    @app.middleware("http")
    async def bind_test_user(request: Request, call_next):
        token = user_context.set_current_user(UserIdentity(id="admin", username="admin"))
        try:
            return await call_next(request)
        finally:
            user_context.reset(token)

    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = StrategyEngine()
    app.state.monitor_engine = MonitorEngine()
    app.include_router(portfolio_router)
    app.include_router(sell_alert_router)
    app.include_router(monitor_rules_router)
    client = TestClient(app)

    manual_rule = {
        "id": "my_manual_rule",
        "name": "我的手工规则",
        "type": "price",
        "scope": "all",
        "symbols": [],
        "conditions": [{"field": "change_pct", "op": ">", "value": 0.05}],
    }
    assert client.post("/api/monitor-rules", json=manual_rule).status_code == 200
    assert client.post("/api/sycee/portfolio/trades", json=_trade()).status_code == 201
    desired = _enable(client).json()["desired_rule"]

    saved = client.post("/api/monitor-rules", json=desired)
    rules = client.get("/api/monitor-rules").json()["rules"]

    assert saved.status_code == 200
    assert {rule["id"] for rule in rules} == {"my_manual_rule", desired["id"]}
    manual = next(rule for rule in rules if rule["id"] == "my_manual_rule")
    managed = next(rule for rule in rules if rule["id"] == desired["id"])
    assert manual["conditions"] == manual_rule["conditions"]
    assert managed["scope"] == "symbols"
    assert managed["symbols"] == ["600519.SH"]
    assert managed["notify_events"] == ["sell_signal"]
