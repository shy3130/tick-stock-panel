from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import settings
from app.services import user_context
from app.services.user_context import UserIdentity
from app.sycee.strategy_tracking import router


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


def _track() -> dict:
    return {
        "strategy_id": "trend_breakout",
        "strategy_name": "趋势突破",
        "symbols": ["600519.sh", "000001.SZ", "600519.SH"],
        "start_date": "2026-01-02",
        "initial_capital": 1_000_000,
        "max_positions": 10,
        "commission_pct": 0.0002,
        "stamp_tax_pct": 0.001,
        "slippage_bps": 5,
        "params": {"lookback": 20},
        "overrides": {"entry_signals": ["signal_breakout"]},
        "note": "观察三个月",
    }


def _observation(end_date: str = "2026-03-31", total_return: float = 0.12) -> dict:
    return {
        "end_date": end_date,
        "run_id": "backtest:run-001",
        "total_return": total_return,
        "annual_return": 0.24,
        "sharpe": 1.35,
        "max_drawdown": -0.08,
        "win_rate": 0.56,
        "trade_count": 18,
        "ending_equity": 1_120_000,
        "elapsed_ms": 820.5,
    }


def test_strategy_track_lifecycle_and_same_day_snapshot_replacement(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    created = client.post("/api/sycee/strategy-tracks", json=_track())
    assert created.status_code == 201
    track = created.json()["track"]
    assert track["symbols"] == ["600519.SH", "000001.SZ"]
    assert track["params"] == {"lookback": 20}
    assert track["status"] == "tracking"

    first = client.post(
        f"/api/sycee/strategy-tracks/{track['id']}/observations",
        json=_observation(),
    )
    assert first.status_code == 200
    assert first.json()["action"] == "created"
    observation_id = first.json()["observation"]["id"]

    replaced = client.post(
        f"/api/sycee/strategy-tracks/{track['id']}/observations",
        json=_observation(total_return=0.15),
    )
    assert replaced.json()["action"] == "replaced"
    assert replaced.json()["observation"]["id"] == observation_id

    paused = client.patch(
        f"/api/sycee/strategy-tracks/{track['id']}",
        json={"status": "paused", "note": "等待样本外数据"},
    )
    assert paused.json()["track"]["status"] == "paused"
    blocked = client.post(
        f"/api/sycee/strategy-tracks/{track['id']}/observations",
        json=_observation("2026-04-30"),
    )
    assert blocked.status_code == 409

    listing = client.get("/api/sycee/strategy-tracks").json()
    assert listing["total"] == 1
    assert len(listing["tracks"][0]["observations"]) == 1
    assert listing["tracks"][0]["observations"][0]["total_return"] == 0.15

    deleted = client.delete(f"/api/sycee/strategy-tracks/{track['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/sycee/strategy-tracks").json()["total"] == 0


def test_strategy_tracks_are_user_scoped(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    client.post("/api/sycee/strategy-tracks", json=_track(), headers={"x-test-user": "admin"})
    alice = client.get("/api/sycee/strategy-tracks", headers={"x-test-user": "alice"})

    assert alice.json() == {"tracks": [], "total": 0}
    assert (tmp_path / "users" / "admin" / "sycee" / "strategy_tracking.json").exists()
    assert not (tmp_path / "users" / "alice" / "sycee" / "strategy_tracking.json").exists()


def test_strategy_tracking_rejects_invalid_plan_and_snapshot_dates(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    invalid_symbol = client.post(
        "/api/sycee/strategy-tracks",
        json={**_track(), "symbols": ["../secret"]},
    )
    assert invalid_symbol.status_code == 422

    track_id = client.post("/api/sycee/strategy-tracks", json=_track()).json()["track"]["id"]
    invalid_date = client.post(
        f"/api/sycee/strategy-tracks/{track_id}/observations",
        json=_observation("2025-12-31"),
    )
    assert invalid_date.status_code == 400
    assert client.get("/api/sycee/strategy-tracks").json()["tracks"][0]["observations"] == []

    empty_update = client.patch(
        f"/api/sycee/strategy-tracks/{track_id}",
        json={"note": None},
    )
    assert empty_update.status_code == 422
