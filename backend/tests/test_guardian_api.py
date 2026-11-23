"""守护 API 契约测试: overview/adopt/drop/rerun/config/runs。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.guardian import router
from app.services.guardian_store import GuardianStore
from app.services.mining_jobs import MiningRunStore


class _Repo:
    def __init__(self, data_dir: Path, latest: date | None = date(2026, 10, 23)) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)
        self._latest = latest

    def latest_enriched_date(self, asset_type="stock"):
        return self._latest


class _StrategyEngine:
    def get(self, strategy_id):
        if strategy_id == "ghost":
            raise ValueError(f"unknown strategy: {strategy_id}")
        return SimpleNamespace(
            meta={"asset_types": ["stock"], "name": f"策略 {strategy_id}", "params": []}
        )


class _Manager:
    def __init__(self, data_dir: Path) -> None:
        self.store = MiningRunStore(data_dir, namespace="guardian")
        self.starts: list[dict] = []

    def start(self, request, fingerprint, force=False, source="manual", run_id=None):
        self.starts.append({"request": request, "force": force, "source": source})
        manifest = self.store.create(request, fingerprint)
        self.store.transition_status(manifest["run_id"], "running")
        return manifest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": False, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4,
                 "lab_mined_slot_limit": 10},
    )

    def fake_set(enabled, watch, retire, k_max, lab_mined_slot_limit=10):
        from app.backtest.guardian import GuardianThresholds

        GuardianThresholds(watch_pct=watch, retire_pct=retire, k_max=k_max)
        if not 0 <= lab_mined_slot_limit <= 50:
            raise ValueError("lab mined slot limit must be between 0 and 50")
        return {
            "guardian_schedule_enabled": enabled,
            "guardian_watch_pct": watch,
            "guardian_retire_pct": retire,
            "guardian_k_max": k_max,
            "lab_mined_slot_limit": lab_mined_slot_limit,
        }

    monkeypatch.setattr(preferences, "set_guardian_schedule", fake_set)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _Repo(tmp_path)
    app.state.strategy_engine = _StrategyEngine()
    app.state.guardian_manager = _Manager(tmp_path)
    return TestClient(app)


def test_overview_empty(client):
    body = client.get("/api/backtest/guardian/overview").json()
    assert body["items"] == []
    assert body["config"]["guardian_schedule_enabled"] is False
    assert body["active_run"] is None


def test_overview_projects_profiles(client, tmp_path):
    store = GuardianStore(tmp_path)
    store.create_baseline(
        "mined_factor_x",
        kind="mining_gate",
        data_as_of="2026-01-09",
        objective="sharpe",
        metrics={"score": 0.9, "n_trades": 70, "max_drawdown": -0.12},
    )
    store.append_check("mined_factor_x", {"verdict": "watch", "degradation_pct": 0.3})
    store.append_check("mined_factor_x", {"verdict": "watch", "degradation_pct": 0.4})
    store.set_status("mined_factor_x", "watch", reason="退化")

    item = client.get("/api/backtest/guardian/overview").json()["items"][0]
    assert item["strategy_id"] == "mined_factor_x"
    assert item["name"] == "策略 mined_factor_x"
    assert item["status"] == "watch"
    assert item["consecutive_watch"] == 2
    assert item["alert"] is True
    assert item["baseline"]["score"] == 0.9
    assert item["last_check"]["verdict"] == "watch"


def test_overview_survives_missing_strategy(client, tmp_path):
    store = GuardianStore(tmp_path)
    store.create_baseline(
        "ghost", kind="mining_gate", data_as_of="2026-01-09",
        objective="sharpe", metrics={"score": 1.0},
    )
    item = client.get("/api/backtest/guardian/overview").json()["items"][0]
    assert item["name"] is None
    assert item["strategy_id"] == "ghost"


def test_adopt_rejects_unknown_strategy_and_objective(client):
    assert client.post(
        "/api/backtest/guardian/ghost/adopt", json={"objective": "sharpe"}
    ).status_code == 404
    assert client.post(
        "/api/backtest/guardian/tune_overnight/adopt", json={"objective": "win_rate"}
    ).status_code == 400


def test_adopt_rejects_missing_enriched(client, tmp_path):
    client.app.state.repo = _Repo(tmp_path, latest=None)
    response = client.post(
        "/api/backtest/guardian/tune_overnight/adopt", json={"objective": "sharpe"}
    )
    assert response.status_code == 400
    assert "enriched" in response.json()["detail"]


def test_adopt_creates_trailing_profile_and_enqueues(client, tmp_path):
    response = client.post(
        "/api/backtest/guardian/tune_overnight/adopt", json={"objective": "sortino"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["run_id"]

    profile = GuardianStore(tmp_path).get("tune_overnight")
    assert profile["baseline"]["kind"] == "trailing_reference"
    assert profile["baseline"]["objective"] == "sortino"
    assert profile["baseline"]["data_as_of"] == "2026-10-23"
    assert profile["baseline"]["metrics"] == {}

    manager = client.app.state.guardian_manager
    assert manager.starts[-1]["request"]["strategy_ids"] == ["tune_overnight"]
    assert manager.starts[-1]["force"] is True


def test_drop_and_rerun(client, tmp_path):
    store = GuardianStore(tmp_path)
    assert client.post("/api/backtest/guardian/nope/drop").status_code == 404
    assert client.post("/api/backtest/guardian/nope/rerun").status_code == 404

    store.create_baseline(
        "mined_factor_x", kind="mining_gate", data_as_of="2026-01-09",
        objective="sharpe", metrics={"score": 0.9},
    )
    response = client.post("/api/backtest/guardian/mined_factor_x/rerun")
    assert response.status_code == 200
    assert response.json()["run_id"]

    response = client.post("/api/backtest/guardian/mined_factor_x/drop")
    assert response.json()["ok"] is True
    assert GuardianStore(tmp_path).get("mined_factor_x") is None


def test_run_all_enqueues(client):
    response = client.post("/api/backtest/guardian/run")
    assert response.status_code == 200
    manager = client.app.state.guardian_manager
    assert manager.starts[-1]["request"]["strategy_ids"] is None


def test_active_run_conflict(client):
    manager = client.app.state.guardian_manager
    manifest = manager.store.create({"x": 1}, {"v": 1})  # 默认 queued = 活跃
    assert manifest["status"] == "queued"
    assert client.post("/api/backtest/guardian/run").status_code == 409


def test_runs_listing_and_detail(client):
    manager = client.app.state.guardian_manager
    manifest = manager.start({"strategy_ids": None}, {"v": 1})
    manager.store.transition_status(manifest["run_id"], "succeeded")
    manager.store.write_summary(manifest["run_id"], {"progress": {"done": 1}})

    items = client.get("/api/backtest/guardian/runs").json()["items"]
    assert items and items[0]["run_id"] == manifest["run_id"]

    detail = client.get(f"/api/backtest/guardian/runs/{manifest['run_id']}").json()
    assert detail["status"] == "succeeded"
    assert detail["summary"]["progress"] == {"done": 1}
    assert client.get("/api/backtest/guardian/runs/missing").status_code == 404


def test_config_patch_validation(client):
    assert client.get("/api/backtest/guardian/config").json()["guardian_k_max"] == 4
    assert client.patch("/api/backtest/guardian/config", json={}).status_code == 400
    # watch >= retire 违反阈值约束 → 400
    assert client.patch(
        "/api/backtest/guardian/config",
        json={"guardian_watch_pct": 0.6, "guardian_retire_pct": 0.5},
    ).status_code == 400

    response = client.patch(
        "/api/backtest/guardian/config",
        json={"guardian_schedule_enabled": True, "guardian_watch_pct": 0.3},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["guardian_schedule_enabled"] is True
    assert body["guardian_watch_pct"] == 0.3
    assert body["guardian_retire_pct"] == 0.5
