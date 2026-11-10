"""自定义因子归档测试 — 可逆标记位 + 列表合并 + 注册表不受影响。"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.factors import router
from app.factors import store
from app.factors.registry import get_factor, unregister_factor


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    with contextlib.suppress(ValueError):
        unregister_factor("uf_arch_test")


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    return TestClient(app)


_DEFINITION = {
    "id": "uf_arch_test",
    "kind": "custom",
    "version": 1,
    "label": "归档测试",
    "group": "自定义",
    "formula": "rank(-ts_sum(change_pct, 5))",
    "lang": "dsl",
    "status": "active",
    "direction": "none",
}


def test_archive_unarchive_roundtrip(tmp_path):
    client = _client(tmp_path)
    store.register_definition(_DEFINITION)  # 模拟启动加载后的注册状态
    store.save_one(Path(tmp_path), _DEFINITION)

    archived = client.post("/api/factors/custom/uf_arch_test/archive")
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    on_disk = next(d for d in store.load_all(Path(tmp_path)) if d["id"] == "uf_arch_test")
    assert on_disk["archived"] is True and on_disk["archived_at"]

    # 归档不注销注册表: 引用它的策略/评分列照常计算
    assert get_factor("uf_arch_test") is not None

    listing = client.get("/api/factors").json()["factors"]
    mine = next(f for f in listing if f["id"] == "uf_arch_test")
    assert mine["archived"] is True

    restored = client.post("/api/factors/custom/uf_arch_test/unarchive")
    assert restored.status_code == 200
    assert restored.json()["archived"] is False
    on_disk = next(d for d in store.load_all(Path(tmp_path)) if d["id"] == "uf_arch_test")
    assert on_disk["archived"] is False and on_disk["archived_at"] is None


def test_archive_unknown_factor_404(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/factors/custom/uf_ghost/archive").status_code == 404


def test_create_writes_archived_false(tmp_path):
    """旧版本创建的定义缺失 archived 字段时, 列表层按 False 合并 (读取端容忍)。"""
    on_disk = {**_DEFINITION}
    on_disk.pop("archived", None)
    store.register_definition(on_disk)  # 进入注册表后才会出现在 GET 列表
    listing = _client(tmp_path).get("/api/factors").json()["factors"]
    mine = next(f for f in listing if f["id"] == "uf_arch_test")
    assert mine["archived"] is False  # 缺字段 → False


def test_backtest_columns_expose_archive_metadata_but_registry_still_computes(tmp_path):
    from app.api.backtest import factor_columns

    store.register_definition(_DEFINITION)
    store.save_one(tmp_path, {**_DEFINITION, "archived": True})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    columns = factor_columns(request)["columns"]
    mine = next(item for item in columns if item["id"] == "uf_arch_test")
    assert mine["archived"] is True
    assert get_factor("uf_arch_test") is not None
