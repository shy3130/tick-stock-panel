"""进行中任务注册表 (跨设备接回) 的服务与 API 测试。

契约: upsert 幂等 (同 id 更新不新增)、容量上限、孤儿 running 回收、
API 生命周期 (PUT/GET/DELETE) 与非法输入拒绝。存取均为小文件原子写。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import task_registry as task_registry_api
from app.services import task_registry


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(task_registry_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def test_upsert_idempotent_and_list_sorted(tmp_path: Path) -> None:
    task_registry.upsert(tmp_path, "backtest:abc", kind="backtest", status="running", payload={"qs": "x=1"})
    task_registry.upsert(
        tmp_path, "backtest:abc", kind="backtest", status="done", payload={"qs": "x=1"}
    )
    task_registry.upsert(tmp_path, "mining:m1", kind="mining", status="running", payload={"run_id": "r1"})

    items = task_registry.list_items(tmp_path)
    assert [it["id"] for it in items] == ["mining:m1", "backtest:abc"]  # updated_at 倒序
    assert items[1]["status"] == "done"  # 同 id 更新而非新增
    assert items[1]["started_at"] > 0

    only_bt = task_registry.list_items(tmp_path, kind="backtest")
    assert [it["id"] for it in only_bt] == ["backtest:abc"]


def test_capacity_trim_keeps_newest(tmp_path: Path) -> None:
    for i in range(task_registry.MAX_ITEMS + 5):
        task_registry.upsert(
            tmp_path, f"backtest:t{i}", kind="backtest", status="done"
        )
        time.sleep(0.001)  # 保证 updated_at 严格递增
    items = task_registry.list_items(tmp_path)
    assert len(items) == task_registry.MAX_ITEMS
    assert items[-1]["id"] == "backtest:t5"  # t0..t4 最旧的被裁掉
    assert items[0]["id"] == f"backtest:t{task_registry.MAX_ITEMS + 4}"


def test_orphan_running_pruned_on_read(tmp_path: Path) -> None:
    import json

    task_registry.upsert(tmp_path, "backtest:old", kind="backtest", status="running")
    # 直接改文件把 updated_at 拨到 25h 前 (模拟进程重启遗留的孤儿 running)
    p = tmp_path / "user_data" / task_registry._FILENAME
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["items"][0]["updated_at"] = time.time() - (task_registry.ORPHAN_RUNNING_SECONDS + 3600)
    p.write_text(json.dumps(doc), encoding="utf-8")

    items = task_registry.list_items(tmp_path)
    assert items == [], "孤儿 running 项应在读取时被回收"
    # 回收已落盘: 重复读不再触发
    assert task_registry.list_items(tmp_path) == []


def test_api_lifecycle(client: TestClient) -> None:
    assert client.get("/api/task-registry").json() == {"items": []}

    r = client.put(
        "/api/task-registry/backtest:k1",
        json={"kind": "backtest", "status": "running", "label": "demo", "payload": {"qs": "a=1"}},
    )
    assert r.status_code == 200 and r.json()["item"]["id"] == "backtest:k1"

    listed = client.get("/api/task-registry", params={"kind": "backtest"}).json()["items"]
    assert len(listed) == 1 and listed[0]["label"] == "demo"

    r = client.put(
        "/api/task-registry/backtest:k1",
        json={"kind": "backtest", "status": "done", "payload": {"qs": "a=1"}},
    )
    assert r.status_code == 200
    assert client.get("/api/task-registry").json()["items"][0]["status"] == "done"

    assert client.delete("/api/task-registry/backtest:k1").json()["removed"] is True
    assert client.get("/api/task-registry").json()["items"] == []
    assert client.delete("/api/task-registry/backtest:k1").json()["removed"] is False


def test_api_rejects_bad_input(client: TestClient) -> None:
    assert client.put(
        "/api/task-registry/bad id!", json={"kind": "backtest", "status": "running"}
    ).status_code == 400  # id 含非法字符
    assert client.put(
        "/api/task-registry/backtest:x", json={"kind": "nope", "status": "running"}
    ).status_code == 400  # 未知 kind
    assert client.put(
        "/api/task-registry/backtest:x", json={"kind": "backtest", "status": "weird"}
    ).status_code == 400  # 未知 status
    assert client.get("/api/task-registry", params={"kind": "nope"}).status_code == 400
