"""自定义信号归档测试 — 可逆标记位 + created_at 治理 + 计算不受影响。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.signals import router
from app.strategy import custom_signals


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    return TestClient(app)


_SIGNAL = {
    "id": "archive_test",
    "name": "归档测试",
    "kind": "entry",
    "conditions": [{"left": "rsi_14", "op": "<", "right": "30"}],
    "enabled": True,
}


def test_archive_unarchive_roundtrip(tmp_path):
    client = _client(tmp_path)
    saved = client.post("/api/custom-signals", json=_SIGNAL)
    assert saved.status_code == 200
    assert saved.json()["signal"]["created_at"]  # 新建补写 created_at
    assert saved.json()["signal"]["archived"] is False

    # 旧客户端兼容: 编辑保存不带 archived/created_at 字段, 盘上值不漂移
    on_disk = custom_signals.load_one(Path(tmp_path), "archive_test")
    legacy_edit = {**_SIGNAL, "name": "归档测试v2"}
    edited = client.post("/api/custom-signals", json=legacy_edit)
    assert edited.status_code == 200
    assert edited.json()["signal"]["created_at"] == on_disk["created_at"]

    archived = client.post("/api/custom-signals/archive_test/archive")
    assert archived.status_code == 200
    assert archived.json()["signal"]["archived"] is True
    assert archived.json()["signal"]["archived_at"]

    # 归档不删除文件, 列表仍返回 (由前端过滤归档项)
    listed = client.get("/api/custom-signals").json()["signals"]
    assert [s["id"] for s in listed] == ["archive_test"]
    assert listed[0]["archived"] is True

    # 归档后仍可正常编译 (已引用它的策略不受影响)
    exprs = custom_signals.build_expressions([listed[0]])
    assert "csg_archive_test" in exprs

    restored = client.post("/api/custom-signals/archive_test/unarchive")
    assert restored.status_code == 200
    assert restored.json()["signal"]["archived"] is False
    assert restored.json()["signal"]["archived_at"] is None
    assert restored.json()["signal"]["created_at"] == on_disk["created_at"]


def test_archive_fail_closed_for_unknown(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/custom-signals/ghost/archive").status_code == 404
    assert client.post("/api/custom-signals/BAD_ID/unarchive").status_code == 400
