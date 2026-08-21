"""F6 选股方案: JSON 存储服务 + /api/screener/screens 四个端点。"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener
from app.services import screener_screens as store


def _conditions():
    return [{"field": "change_pct", "op": ">", "value": 0}]


_ID_RE = re.compile(r"^[0-9a-f]{12}$")


# ── 存储层 ──────────────────────────────────────────────────────────


def test_create_persists_atomic_and_lists(tmp_path):
    record = store.create_screen(tmp_path, name="强势股", conditions=_conditions())

    assert _ID_RE.match(record["id"])
    assert set(record) == {"id", "name", "conditions", "order_by", "limit", "group_logic", "created_at", "updated_at"}
    assert record["conditions"] == _conditions()
    assert record["group_logic"] == "and"  # F14: 默认 and, 旧方案行为不变
    assert record["order_by"] is None and record["limit"] is None

    # 原子写: 文件可解析, 无 .tmp 残留
    path = tmp_path / "user_data" / "screener_screens.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"screens": [record]}
    assert not list(path.parent.glob("*.tmp"))
    assert store.list_screens(tmp_path) == [record]


def test_create_strips_and_normalizes_order_by_limit(tmp_path):
    record = store.create_screen(
        tmp_path,
        name="  沿革  ",
        conditions=_conditions(),
        order_by={"field": "close", "direction": "asc"},
        limit=50,
    )
    assert record["name"] == "沿革"
    assert record["order_by"] == {"field": "close", "direction": "asc"}
    assert record["limit"] == 50


def test_update_keeps_created_refreshes_updated(tmp_path, monkeypatch):
    clock = iter(["2026-08-20T01:00:00+00:00", "2026-08-20T02:00:00+00:00"])
    monkeypatch.setattr(store, "_now", lambda: next(clock))
    created = store.create_screen(tmp_path, name="A", conditions=_conditions())

    updated = store.update_screen(tmp_path, created["id"], name="B", conditions=_conditions())

    assert updated["name"] == "B"
    assert updated["created_at"] == created["created_at"] == "2026-08-20T01:00:00+00:00"
    assert updated["updated_at"] == "2026-08-20T02:00:00+00:00"
    assert store.list_screens(tmp_path) == [updated]


def test_update_and_delete_missing_raise_not_found(tmp_path):
    with pytest.raises(store.ScreenNotFoundError):
        store.update_screen(tmp_path, "deadbeef0000", name="x", conditions=_conditions())
    with pytest.raises(store.ScreenNotFoundError):
        store.delete_screen(tmp_path, "deadbeef0000")


def test_delete_removes_only_target(tmp_path):
    first = store.create_screen(tmp_path, name="A", conditions=_conditions())
    second = store.create_screen(tmp_path, name="B", conditions=_conditions())

    store.delete_screen(tmp_path, first["id"])

    assert [s["id"] for s in store.list_screens(tmp_path)] == [second["id"]]


@pytest.mark.parametrize("name", ["", "   ", "x" * 41])
def test_name_validation(tmp_path, name):
    with pytest.raises(store.ScreenStoreError) as exc:
        store.create_screen(tmp_path, name=name, conditions=_conditions())
    assert exc.value.code == "invalid_name"
    # 校验失败不落盘
    assert not (tmp_path / "user_data" / "screener_screens.json").exists()


@pytest.mark.parametrize(
    "conditions,order_by,limit",
    [
        ([], None, None),  # 空 conditions
        ([{"field": "change_pct"}], None, None),  # 缺 op
        ([{"field": "change_pct", "op": "between", "value": 1}], None, None),  # between 形状
        ([{"field": "change_pct", "op": ">", "value": 0}] * 21, None, None),  # 超过 20 条
        (_conditions(), {"direction": "up"}, None),  # order_by 非法方向
        (_conditions(), None, 0),  # limit 越界
        (_conditions(), None, 501),
    ],
)
def test_conditions_order_by_limit_validation(tmp_path, conditions, order_by, limit):
    with pytest.raises(store.ScreenStoreError) as exc:
        store.create_screen(tmp_path, name="A", conditions=conditions, order_by=order_by, limit=limit)
    assert exc.value.code == "invalid_conditions"


def test_max_50_screens(tmp_path):
    for i in range(store.MAX_SCREENS):
        store.create_screen(tmp_path, name=f"S{i}", conditions=_conditions())
    with pytest.raises(store.ScreenStoreError) as exc:
        store.create_screen(tmp_path, name="超额", conditions=_conditions())
    assert exc.value.code == "screen_limit"
    assert len(store.list_screens(tmp_path)) == store.MAX_SCREENS


def test_corrupt_file_treated_as_empty(tmp_path):
    path = store.store_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    assert store.list_screens(tmp_path) == []


# ── API 层 ──────────────────────────────────────────────────────────


class _Repo:
    store = SimpleNamespace(data_dir=Path("."))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(_Repo, "store", SimpleNamespace(data_dir=tmp_path))
    app = FastAPI()
    app.state.repo = _Repo()
    app.include_router(screener.router)
    return TestClient(app)


def test_screens_crud_roundtrip(client, tmp_path):
    # 空 → 空列表
    assert client.get("/api/screener/screens").json() == {"screens": []}

    created = client.post(
        "/api/screener/screens",
        json={"name": "强势股", "conditions": _conditions(), "limit": 30},
    )
    assert created.status_code == 201
    record = created.json()
    assert _ID_RE.match(record["id"]) and record["limit"] == 30

    # GET 列表可见 (F16: 每项附 strategy_supported/unsupported_fields)
    listed = client.get("/api/screener/screens")
    assert listed.status_code == 200
    item = listed.json()["screens"][0]
    assert {k: v for k, v in item.items() if k in record} == record
    assert item["strategy_supported"] is True  # change_pct 纯面板字段
    assert item["unsupported_fields"] == []

    # PUT 更新
    updated = client.put(
        f"/api/screener/screens/{record['id']}",
        json={"name": "更名", "conditions": _conditions()},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "更名"
    assert updated.json()["created_at"] == record["created_at"]

    # DELETE 204 → 再删 404
    assert client.delete(f"/api/screener/screens/{record['id']}").status_code == 204
    assert client.get("/api/screener/screens").json() == {"screens": []}
    assert client.delete(f"/api/screener/screens/{record['id']}").status_code == 404
    assert client.put(
        f"/api/screener/screens/{record['id']}",
        json={"name": "x", "conditions": _conditions()},
    ).status_code == 404


def test_screens_validation_maps_to_400(client):
    bad_name = client.post(
        "/api/screener/screens", json={"name": "", "conditions": _conditions()}
    )
    assert bad_name.status_code == 400
    assert bad_name.json()["detail"]["code"] == "invalid_name"

    bad_conditions = client.post(
        "/api/screener/screens", json={"name": "A", "conditions": []}
    )
    assert bad_conditions.status_code == 400
    assert bad_conditions.json()["detail"]["code"] == "invalid_conditions"
