"""仓位策略 API — 列表/模板/校验/保存/删除/回测请求 400 校验。

沿用 tests/test_job_stall_and_cancel.py 的 mini-app 模式: 只挂需要的路由,
不进主应用 lifespan (避免拉起数据源/调度器)。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import position_policies as position_policies_api
from app.backtest import position_policies as pp
from app.services import preferences


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    from app.api import backtest as backtest_api
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences, "load", lambda: {})

    app = FastAPI()
    app.include_router(backtest_api.router)
    app.include_router(position_policies_api.router)
    app.state.data_dir = tmp_path
    pp.invalidate_cache()
    return TestClient(app)


def test_templates_endpoint(client: TestClient) -> None:
    r = client.get("/api/backtest/position-policies/templates")
    assert r.status_code == 200
    keys = [t["key"] for t in r.json()["templates"]]
    assert keys == ["blank", "vol_target", "drawdown"]


def test_validate_rejects_bad_code(client: TestClient) -> None:
    r = client.post(
        "/api/backtest/position-policies/validate",
        json={"label": "坏策略", "code": "def compute(ctx):\n    import os\n    return 1\n"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert any("安全校验" in i for i in body["issues"])


def test_validate_ok_for_template(client: TestClient) -> None:
    r = client.post(
        "/api/backtest/position-policies/validate",
        json={
            "label": "波动率目标",
            "params": pp.TEMPLATE_VOL_TARGET["meta"]["params"],
            "code": pp.TEMPLATE_VOL_TARGET["code"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"], body["issues"]
    assert body["smoke"]["weights"] and abs(sum(body["smoke"]["weights"]) - 1.0) < 1e-3


def test_save_get_list_delete_cycle(client: TestClient, tmp_path: Path) -> None:
    r = client.post(
        "/api/backtest/position-policies",
        json={
            "label": "我的波动率仓位",
            "description": "测试",
            "params": pp.TEMPLATE_VOL_TARGET["meta"]["params"],
            "code": pp.TEMPLATE_VOL_TARGET["code"],
        },
    )
    assert r.status_code == 200
    policy_id = r.json()["id"]
    assert policy_id.startswith("up_")

    got = client.get(f"/api/backtest/position-policies/{policy_id}").json()
    assert got["meta"]["label"] == "我的波动率仓位"
    assert "def compute(ctx)" in got["code"]

    listed = client.get("/api/backtest/position-policies").json()
    assert [p["id"] for p in listed["policies"]] == [policy_id]

    assert client.delete(f"/api/backtest/position-policies/{policy_id}").status_code == 200
    assert client.get("/api/backtest/position-policies").json()["policies"] == []


def test_backtest_run_with_unknown_policy_returns_400(client: TestClient) -> None:
    r = client.post(
        "/api/backtest/strategy/run",
        json={"strategy_id": "any", "position_sizing": "policy", "position_policy": "up_missing"},
    )
    assert r.status_code == 400
    assert "仓位策略不存在" in r.json()["detail"]


def test_get_and_delete_reject_path_traversal_ids(client: TestClient, tmp_path: Path) -> None:
    """policy_id 直接拼文件路径: 非法 ID (路径穿越) 必须 400 fail-closed。

    Windows 下 %5C 解码为分隔符, `..%5C..%5Cx` 可越界读/删目录外 .py 文件。
    """
    victim = tmp_path / "victim.py"
    victim.write_text("SECRET = 1", encoding="utf-8")

    # 注: 未编码 ../ 被路径归一化、含 / 的被路由分段挡掉; 编码 %5C 才是真实攻击面
    for bad_id in ("..%5C..%5Cvictim", "UP_BAD", "a", "..%2Eevil"):
        resp = client.get(f"/api/backtest/position-policies/{bad_id}")
        assert resp.status_code == 400, (bad_id, resp.status_code)
        resp = client.delete(f"/api/backtest/position-policies/{bad_id}")
        assert resp.status_code == 400, (bad_id, resp.status_code)

    assert victim.exists(), "越界删除不得发生"
