from __future__ import annotations

from copy import deepcopy

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import settings
from app.services import user_context
from app.services.user_context import UserIdentity
from app.sycee import data_backup
from app.sycee.data_backup import router as data_backup_router
from app.sycee.portfolio import router as portfolio_router
from app.sycee.portfolio_sell_alert import router as sell_alert_router
from app.sycee.research_ledger import router as research_router
from app.sycee.strategy_tracking import router as strategy_tracking_router
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
    app.include_router(sell_alert_router)
    app.include_router(research_router)
    app.include_router(strategy_tracking_router)
    app.include_router(trade_reviews_router)
    app.include_router(data_backup_router)
    return TestClient(app)


def _trade(symbol: str = "600519.SH") -> dict:
    return {
        "symbol": symbol,
        "name": "贵州茅台",
        "side": "buy",
        "quantity": 100,
        "price": 1500,
        "fees": 5,
        "trade_date": "2026-07-20",
        "note": "趋势突破",
    }


def _review() -> dict:
    return {
        "strategy_id": "trend_breakout",
        "entry_reason": "突破后回踩确认",
        "expectation": "趋势延续",
        "invalidation": "跌破前低",
        "exit_reason": "",
        "conclusion": "",
        "mistake_tags": [],
    }


def _research(title: str = "贵州茅台跟踪") -> dict:
    return {
        "title": title,
        "subject_type": "stock",
        "subject": "600519.SH",
        "thesis": "基本面改善",
        "evidence": [],
        "counter_evidence": [],
        "invalidation": "业绩不及预期",
        "plan": "等待财报",
        "status": "tracking",
        "tags": ["白酒"],
    }


def _strategy_track() -> dict:
    return {
        "strategy_id": "trend_breakout",
        "strategy_name": "趋势突破",
        "symbols": ["600519.SH"],
        "start_date": "2026-07-01",
        "initial_capital": 1_000_000,
        "max_positions": 10,
        "commission_pct": 0.0002,
        "stamp_tax_pct": 0.001,
        "slippage_bps": 5,
        "params": {"lookback": 20},
        "overrides": {"entry_signals": ["signal_breakout"]},
        "note": "样本外跟踪",
    }


def _seed_all_sections(client: TestClient) -> str:
    trade = client.post("/api/sycee/portfolio/trades", json=_trade()).json()["trade"]
    client.put(f"/api/sycee/trade-reviews/{trade['id']}", json=_review())
    client.post("/api/sycee/research", json=_research())
    client.put(
        "/api/sycee/portfolio/sell-alert",
        json={"enabled": False, "strategy_id": "trend_breakout", "webhook_channels": []},
    )
    client.post("/api/sycee/strategy-tracks", json=_strategy_track())
    return trade["id"]


def test_backup_round_trip_is_user_scoped_and_keeps_safety_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    trade_id = _seed_all_sections(client)

    exported = client.get("/api/sycee/data-backup").json()
    assert exported["format"] == "sycee-user-data"
    assert exported["version"] == 1
    assert len(exported["data"]["portfolio"]["trades"]) == 1
    assert exported["data"]["trade_reviews"]["reviews"][0]["trade_id"] == trade_id
    assert len(exported["data"]["research_ledger"]["entries"]) == 1
    assert len(exported["data"]["strategy_tracking"]["tracks"]) == 1
    assert exported["data"]["portfolio_sell_alert"]["config"]["strategy_id"] == "trend_breakout"

    client.post("/api/sycee/portfolio/trades", json=_trade("000001.SZ"))
    client.post("/api/sycee/research", json=_research("第二条记录"))
    track_id = client.get("/api/sycee/strategy-tracks").json()["tracks"][0]["id"]
    client.delete(f"/api/sycee/strategy-tracks/{track_id}")
    restored = client.post(
        "/api/sycee/data-backup/restore",
        json={"confirmation": "RESTORE_SYCEE_DATA", "backup": exported},
    )

    assert restored.status_code == 200
    assert client.get("/api/sycee/portfolio").json()["summary"]["trade_count"] == 1
    assert client.get("/api/sycee/research").json()["total"] == 1
    assert client.get("/api/sycee/strategy-tracks").json()["total"] == 1
    backup_id = restored.json()["safety_backup_id"]
    safety_dir = tmp_path / "users" / "admin" / "sycee" / "backups" / backup_id
    assert (safety_dir / "manifest.json").exists()
    assert (safety_dir / "portfolio.json").exists()
    assert (safety_dir / "research_ledger.json").exists()

    alice = client.get("/api/sycee/data-backup", headers={"x-test-user": "alice"}).json()
    assert all(value is None for value in alice["data"].values())


def test_invalid_backup_is_rejected_before_current_data_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    _seed_all_sections(client)
    exported = client.get("/api/sycee/data-backup").json()
    invalid = deepcopy(exported)
    invalid["data"]["portfolio"]["trades"].append(
        {
            **_trade(),
            "id": "trade_00000000000000000000000000000002",
            "side": "sell",
            "quantity": 1000,
            "created_at": "2026-07-21T00:00:00+00:00",
            "updated_at": "2026-07-21T00:00:00+00:00",
            "trade_date": "2026-07-21",
        }
    )

    response = client.post(
        "/api/sycee/data-backup/restore",
        json={"confirmation": "RESTORE_SYCEE_DATA", "backup": invalid},
    )

    assert response.status_code == 400
    assert client.get("/api/sycee/portfolio").json()["summary"]["trade_count"] == 1
    backups = tmp_path / "users" / "admin" / "sycee" / "backups"
    assert not backups.exists()


def test_restore_rolls_back_when_a_file_replace_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    _seed_all_sections(client)
    incoming = client.get("/api/sycee/data-backup").json()
    incoming["data"]["portfolio"]["trades"] = []
    incoming["data"]["research_ledger"]["entries"] = []

    real_replace = data_backup.os.replace
    restore_replaces = 0

    def fail_second_restore(source, target):
        nonlocal restore_replaces
        if str(source).endswith(".restore"):
            restore_replaces += 1
            if restore_replaces == 2:
                raise OSError("simulated restore failure")
        return real_replace(source, target)

    monkeypatch.setattr(data_backup.os, "replace", fail_second_restore)
    response = client.post(
        "/api/sycee/data-backup/restore",
        json={"confirmation": "RESTORE_SYCEE_DATA", "backup": incoming},
    )

    assert response.status_code == 500
    assert "已回滚" in response.json()["detail"]
    assert client.get("/api/sycee/portfolio").json()["summary"]["trade_count"] == 1
    assert client.get("/api/sycee/research").json()["total"] == 1


def test_restore_requires_explicit_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    exported = client.get("/api/sycee/data-backup").json()

    response = client.post(
        "/api/sycee/data-backup/restore",
        json={"confirmation": "yes", "backup": exported},
    )

    assert response.status_code == 422


def test_legacy_backup_without_strategy_tracking_keeps_current_plans(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    client.post("/api/sycee/strategy-tracks", json=_strategy_track())
    exported = client.get("/api/sycee/data-backup").json()
    del exported["data"]["strategy_tracking"]

    restored = client.post(
        "/api/sycee/data-backup/restore",
        json={"confirmation": "RESTORE_SYCEE_DATA", "backup": exported},
    )

    assert restored.status_code == 200
    assert client.get("/api/sycee/strategy-tracks").json()["total"] == 1


def test_backup_rejects_strategy_snapshot_before_plan_start(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    track = client.post("/api/sycee/strategy-tracks", json=_strategy_track()).json()["track"]
    client.post(
        f"/api/sycee/strategy-tracks/{track['id']}/observations",
        json={
            "end_date": "2026-07-20",
            "run_id": "abc123",
            "total_return": 0.1,
            "annual_return": 0.2,
            "sharpe": 1.1,
            "max_drawdown": -0.05,
            "win_rate": 0.5,
            "trade_count": 10,
            "ending_equity": 1_100_000,
            "elapsed_ms": 100,
        },
    )
    invalid = client.get("/api/sycee/data-backup").json()
    invalid["data"]["strategy_tracking"]["tracks"][0]["observations"][0][
        "end_date"
    ] = "2026-06-30"

    response = client.post(
        "/api/sycee/data-backup/restore",
        json={"confirmation": "RESTORE_SYCEE_DATA", "backup": invalid},
    )

    assert response.status_code == 400
    assert client.get("/api/sycee/strategy-tracks").json()["total"] == 1
