from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pytest import approx

from app.config import settings
from app.services import user_context
from app.services.user_context import UserIdentity
from app.sycee.portfolio import router


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

    app.include_router(router)
    return TestClient(app)


def _trade(
    *,
    side: str = "buy",
    quantity: float = 100,
    price: float = 10,
    trade_date: str = "2026-01-02",
    fees: float = 0,
) -> dict:
    return {
        "symbol": "600519.sh",
        "name": "贵州茅台",
        "side": side,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "trade_date": trade_date,
        "note": "测试流水",
    }


def test_portfolio_trade_crud_and_weighted_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    first = client.post("/api/sycee/portfolio/trades", json=_trade(fees=5))
    assert first.status_code == 201
    first_id = first.json()["trade"]["id"]
    assert first.json()["trade"]["symbol"] == "600519.SH"

    second = client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(quantity=50, price=12, trade_date="2026-01-03"),
    )
    second_id = second.json()["trade"]["id"]

    sold = client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(side="sell", quantity=40, price=15, fees=2, trade_date="2026-01-04"),
    )
    sold_id = sold.json()["trade"]["id"]

    portfolio = client.get("/api/sycee/portfolio").json()
    assert [trade["id"] for trade in portfolio["trades"]] == [sold_id, second_id, first_id]
    assert portfolio["summary"] == {
        "position_count": 1,
        "trade_count": 3,
        "cost_value": 1177.0,
        "realized_pnl": 170.0,
    }
    position = portfolio["positions"][0]
    assert position["quantity"] == 110
    assert position["average_cost"] == approx(10.7)
    assert position["cost_value"] == approx(1177)
    assert position["realized_pnl"] == approx(170)

    edited = client.patch(
        f"/api/sycee/portfolio/trades/{sold_id}",
        json={"quantity": 20},
    )
    assert edited.status_code == 200
    edited_portfolio = edited.json()["portfolio"]
    assert edited_portfolio["positions"][0]["quantity"] == 130
    assert edited_portfolio["summary"]["realized_pnl"] == approx(84)

    deleted = client.delete(f"/api/sycee/portfolio/trades/{second_id}")
    assert deleted.status_code == 200
    deleted_portfolio = deleted.json()["portfolio"]
    assert deleted_portfolio["positions"][0]["quantity"] == 80
    assert deleted_portfolio["positions"][0]["average_cost"] == approx(10.05)
    assert deleted_portfolio["summary"]["realized_pnl"] == approx(97)


def test_portfolio_rejects_mutations_that_create_historical_oversell(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    bought = client.post("/api/sycee/portfolio/trades", json=_trade()).json()["trade"]
    sold = client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(side="sell", quantity=80, price=12, trade_date="2026-01-03"),
    )
    assert sold.status_code == 201

    oversell = client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(side="sell", quantity=30, price=12, trade_date="2026-01-04"),
    )
    assert oversell.status_code == 409
    assert "可卖数量" in oversell.json()["detail"]

    invalid_edit = client.patch(
        f"/api/sycee/portfolio/trades/{bought['id']}",
        json={"quantity": 50},
    )
    assert invalid_edit.status_code == 409

    invalid_delete = client.delete(f"/api/sycee/portfolio/trades/{bought['id']}")
    assert invalid_delete.status_code == 409

    unchanged = client.get("/api/sycee/portfolio").json()
    assert unchanged["summary"]["trade_count"] == 2
    assert unchanged["positions"][0]["quantity"] == 20


def test_portfolio_is_isolated_per_user(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    admin_headers = {"x-test-user": "admin"}
    alice_headers = {"x-test-user": "alice"}
    client.post("/api/sycee/portfolio/trades", headers=admin_headers, json=_trade())
    client.post(
        "/api/sycee/portfolio/trades",
        headers=alice_headers,
        json={**_trade(quantity=20), "symbol": "000001.sz", "name": "平安银行"},
    )

    admin = client.get("/api/sycee/portfolio", headers=admin_headers).json()
    alice = client.get("/api/sycee/portfolio", headers=alice_headers).json()
    assert [position["symbol"] for position in admin["positions"]] == ["600519.SH"]
    assert [position["symbol"] for position in alice["positions"]] == ["000001.SZ"]
    assert (tmp_path / "users" / "admin" / "sycee" / "portfolio.json").exists()
    assert (tmp_path / "users" / "alice" / "sycee" / "portfolio.json").exists()


def test_portfolio_validates_trade_input(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    invalid_symbol = client.post(
        "/api/sycee/portfolio/trades",
        json={**_trade(), "symbol": "../secret"},
    )
    invalid_date = client.post(
        "/api/sycee/portfolio/trades",
        json={**_trade(), "trade_date": "2026-02-30"},
    )
    invalid_quantity = client.post(
        "/api/sycee/portfolio/trades",
        json={**_trade(), "quantity": 0},
    )

    assert invalid_symbol.status_code == 422
    assert invalid_date.status_code == 422
    assert invalid_quantity.status_code == 422
