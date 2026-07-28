from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pytest import approx

from app.config import settings
from app.services import user_context
from app.services.user_context import UserIdentity
from app.sycee.portfolio import router as portfolio_router
from app.sycee.trade_reviews import _derive_attributions
from app.sycee.trade_reviews import router as trade_reviews_router


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
    app.include_router(trade_reviews_router)
    return TestClient(app)


def _trade(
    *,
    side: str = "buy",
    quantity: float = 100,
    price: float = 10,
    fees: float = 0,
    trade_date: str = "2026-01-02",
    symbol: str = "600519.SH",
) -> dict:
    return {
        "symbol": symbol,
        "name": "贵州茅台" if symbol == "600519.SH" else symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "trade_date": trade_date,
        "note": "",
    }


def _review(**overrides) -> dict:
    return {
        "strategy_id": "trend_breakout",
        "entry_reason": "突破后回踩确认",
        "expectation": "趋势延续",
        "invalidation": "跌破前低",
        "exit_reason": "触发止盈",
        "conclusion": "计划执行完整",
        "mistake_tags": [],
        **overrides,
    }


def test_trade_review_crud_and_live_moving_cost_attribution(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    client.post("/api/sycee/portfolio/trades", json=_trade(fees=5))
    client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(quantity=50, price=12, trade_date="2026-01-03"),
    )
    sold = client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(side="sell", quantity=40, price=15, fees=2, trade_date="2026-01-12"),
    ).json()["trade"]

    saved = client.put(
        f"/api/sycee/trade-reviews/{sold['id']}",
        json=_review(mistake_tags=["early_exit", "early_exit"]),
    )
    result = client.get("/api/sycee/trade-reviews").json()
    item = next(row for row in result["items"] if row["trade"]["id"] == sold["id"])

    assert saved.status_code == 200
    assert saved.json()["review"]["mistake_tags"] == ["early_exit"]
    assert item["attribution"]["cost_basis"] == approx(428)
    assert item["attribution"]["realized_pnl"] == approx(170)
    assert item["attribution"]["return_pct"] == approx(170 / 428)
    assert item["attribution"]["holding_days"] == 10
    assert item["attribution"]["pnl_result"] == "profit"
    assert result["summary"] == {
        "trade_count": 3,
        "reviewed_count": 1,
        "sell_count": 1,
        "reviewed_sell_count": 1,
        "orphaned_count": 0,
    }

    edited = client.patch(
        f"/api/sycee/portfolio/trades/{sold['id']}",
        json={"price": 16},
    )
    refreshed = client.get("/api/sycee/trade-reviews").json()
    refreshed_item = next(row for row in refreshed["items"] if row["trade"]["id"] == sold["id"])
    assert edited.status_code == 200
    assert refreshed_item["attribution"]["realized_pnl"] == approx(210)
    assert refreshed_item["review"]["conclusion"] == "计划执行完整"

    deleted = client.delete(f"/api/sycee/trade-reviews/{sold['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/sycee/trade-reviews").json()["summary"]["reviewed_count"] == 0


def test_holding_period_resets_after_a_position_is_fully_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    client.post("/api/sycee/portfolio/trades", json=_trade(trade_date="2026-01-01"))
    first_sell = client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(side="sell", trade_date="2026-01-03"),
    ).json()["trade"]
    client.post("/api/sycee/portfolio/trades", json=_trade(trade_date="2026-02-01"))
    second_sell = client.post(
        "/api/sycee/portfolio/trades",
        json=_trade(side="sell", trade_date="2026-02-02"),
    ).json()["trade"]

    items = client.get("/api/sycee/trade-reviews").json()["items"]
    by_id = {item["trade"]["id"]: item for item in items}

    assert by_id[first_sell["id"]]["attribution"]["holding_days"] == 2
    assert by_id[second_sell["id"]]["attribution"]["holding_days"] == 1


def test_same_second_attribution_uses_portfolio_replay_order():
    created_at = "2026-07-28T01:02:03+00:00"
    buy = {
        "id": "trade_ffffffffffffffffffffffffffffffff",
        "created_at": created_at,
        **_trade(),
    }
    sell = {
        "id": "trade_00000000000000000000000000000000",
        "created_at": created_at,
        **_trade(side="sell", quantity=40, price=15),
    }

    attributions = _derive_attributions([sell, buy])

    assert attributions[sell["id"]]["cost_basis"] == 400
    assert attributions[sell["id"]]["realized_pnl"] == 200


def test_deleted_trade_keeps_review_as_an_orphan_until_user_removes_it(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    trade = client.post("/api/sycee/portfolio/trades", json=_trade()).json()["trade"]
    client.put(f"/api/sycee/trade-reviews/{trade['id']}", json=_review())

    assert client.delete(f"/api/sycee/portfolio/trades/{trade['id']}").status_code == 200
    result = client.get("/api/sycee/trade-reviews").json()

    assert result["summary"]["orphaned_count"] == 1
    assert result["items"] == [
        {"trade": None, "attribution": None, "review": result["items"][0]["review"]}
    ]
    assert result["items"][0]["review"]["trade_id"] == trade["id"]
    assert client.delete(f"/api/sycee/trade-reviews/{trade['id']}").status_code == 200


def test_trade_reviews_are_isolated_per_user(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    admin_headers = {"x-test-user": "admin"}
    alice_headers = {"x-test-user": "alice"}
    admin_trade = client.post(
        "/api/sycee/portfolio/trades",
        headers=admin_headers,
        json=_trade(),
    ).json()["trade"]
    alice_trade = client.post(
        "/api/sycee/portfolio/trades",
        headers=alice_headers,
        json=_trade(symbol="000001.SZ"),
    ).json()["trade"]
    client.put(
        f"/api/sycee/trade-reviews/{admin_trade['id']}",
        headers=admin_headers,
        json=_review(strategy_id="trend_breakout"),
    )
    client.put(
        f"/api/sycee/trade-reviews/{alice_trade['id']}",
        headers=alice_headers,
        json=_review(strategy_id="ma_cross"),
    )

    admin = client.get("/api/sycee/trade-reviews", headers=admin_headers).json()
    alice = client.get("/api/sycee/trade-reviews", headers=alice_headers).json()

    assert admin["items"][0]["review"]["strategy_id"] == "trend_breakout"
    assert alice["items"][0]["review"]["strategy_id"] == "ma_cross"
    assert (tmp_path / "users" / "admin" / "sycee" / "trade_reviews.json").exists()
    assert (tmp_path / "users" / "alice" / "sycee" / "trade_reviews.json").exists()


def test_trade_review_validates_content_and_trade_id(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    trade = client.post("/api/sycee/portfolio/trades", json=_trade()).json()["trade"]

    empty = client.put(
        f"/api/sycee/trade-reviews/{trade['id']}",
        json={
            "strategy_id": "",
            "entry_reason": "",
            "expectation": "",
            "invalidation": "",
            "exit_reason": "",
            "conclusion": "",
            "mistake_tags": [],
        },
    )
    missing = client.put(
        "/api/sycee/trade-reviews/trade_00000000000000000000000000000000",
        json=_review(),
    )
    invalid = client.put("/api/sycee/trade-reviews/not-a-trade", json=_review())

    assert empty.status_code == 422
    assert missing.status_code == 404
    assert invalid.status_code == 400
