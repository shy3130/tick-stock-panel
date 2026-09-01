from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.research as research_api
from app.api.research_runs import router as runs_router

OLD_FACTOR_POSTS = [
    "/api/research/factors/volume-breakout/evaluate",
    "/api/research/factors/mtf-direction/evaluate",
    "/api/research/factors/n-shape/evaluate",
    "/api/research/factors/n-shape-pullback-depth/evaluate",
    "/api/research/factors/negative-exclusion/evaluate",
    "/api/research/factors/daily-open-anchor/evaluate",
    "/api/research/factors/zuoyi-defense/evaluate",
    "/api/research/factors/single-yang-no-break/evaluate",
    "/api/research/factors/macd-stages/evaluate",
    "/api/research/factors/weak-to-strong/evaluate",
    "/api/research/factors/hold-firm-patterns/evaluate",
    "/api/research/factors/doji-patterns/evaluate",
    "/api/research/factors/chip-peak-patterns/evaluate",
    "/api/research/factors/weekly-flagpole/evaluate",
    "/api/research/escape-windows/evaluate",
    "/api/research/factors/dugu-trend/evaluate",
    "/api/research/factors/pre-surge-features/evaluate",
    "/api/research/factors/escape-risk/evaluate",
    "/api/research/factors/mera-routing/evaluate",
]

OLD_CAPABILITY_GETS = [
    "/api/research/negative-exclusion",
    "/api/research/daily-open-anchor",
    "/api/research/zuoyi-defense",
    "/api/research/single-yang-no-break",
    "/api/research/macd-stages",
    "/api/research/hold-firm-patterns",
    "/api/research/doji-patterns",
    "/api/research/weekly-flagpole",
    "/api/research/escape-windows",
    "/api/research/escape-risk",
]


def client(tmp_path):
    app = FastAPI()
    app.state.repo = type("Repo", (), {"store": type("Store", (), {"data_dir": tmp_path})()})()
    app.include_router(research_api.router)
    app.include_router(runs_router)
    return TestClient(app)


def test_legacy_factor_routes_are_removed(tmp_path):
    c = client(tmp_path)
    for path in OLD_FACTOR_POSTS:
        assert c.post(path, json={}).status_code == 404
    for path in OLD_CAPABILITY_GETS:
        assert c.get(path).status_code == 404


def test_unified_factor_catalog_replaces_legacy_evaluators(tmp_path):
    response = client(tmp_path).get("/api/research/factors")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 19
    assert {item["id"] for item in response.json()["items"]} == {
        "n-shape",
        "mtf-direction",
        "weak-to-strong",
        "volume-breakout",
        "macd-arms",
        "single-yang-no-break",
        "zuoyi-defense",
        "daily-open-anchor",
        "hold-firm",
        "dugu-trend",
        "mera",
        "pre-surge",
        "escape-risk",
        "n-depth",
        "negative-exclusion",
        "doji-patterns",
        "chip-peak-patterns",
        "weekly-flagpole",
        "escape-windows",
    }
