"""导航分组：版本、旧配置兼容、原子保存与看板固定。"""
from __future__ import annotations

import copy

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    yield
    preferences._invalidate_cache()


def layout():
    return {"v": 1, "items": [
        {"type": "page", "id": "/"},
        {"type": "group", "id": "group:research", "label": "研究分析", "children": [
            {"type": "page", "id": "/stock-analysis"},
            {"type": "page", "id": "/analysis/auction"},
        ]},
        {"type": "page", "id": "/settings"},
    ]}


def test_old_preferences_and_roundtrip():
    preferences.save({"nav_order": ["/watchlist", "/"], "nav_hidden": ["/monitor"]})
    assert preferences.get_nav_layout() is None
    saved = preferences.set_nav_layout(layout(), ["/", "/monitor"])
    assert saved == layout()
    assert preferences.get_nav_layout() == saved
    assert preferences.get_nav_order() == ["/", "/stock-analysis", "/analysis/auction", "/settings"]
    assert preferences.get_nav_hidden() == ["/monitor"]
    saved["items"].clear()
    assert preferences.get_nav_layout() == layout()


@pytest.mark.parametrize("change", [
    lambda x: x.update(v=2),
    lambda x: x["items"].append({"type": "page", "id": "/settings"}),
    lambda x: x["items"][1].update(label=" "),
    lambda x: x["items"][1]["children"].append({"type": "page", "id": "/"}),
    lambda x: x["items"][1]["children"].append({"type": "group", "id": "group:nested", "label": "nested", "children": []}),
    lambda x: x["items"].append({"type": "page", "id": "https://example.test"}),
    lambda x: x["items"].append({"type": "page", "id": "//example.test"}),
])
def test_reject_invalid_layout_without_partial_write(change):
    preferences.set_nav_layout(layout(), ["/monitor"])
    before = preferences.load()
    invalid = copy.deepcopy(layout())
    change(invalid)
    with pytest.raises(ValueError):
        preferences.set_nav_layout(invalid, [])
    assert preferences.load() == before


def test_unknown_pages_are_preserved_and_restore_flat_keeps_order():
    value = layout()
    value["items"][1]["children"].append({"type": "page", "id": "/company/offline"})
    preferences.set_nav_layout(value, ["/monitor"])
    assert preferences.get_nav_layout() == value
    preferences.set_nav_layout(None, None)
    assert preferences.get_nav_layout() is None
    assert "/company/offline" in preferences.get_nav_order()
    assert preferences.get_nav_hidden() == ["/monitor"]


def test_future_version_is_not_overwritten():
    preferences.save({"nav_layout": {"v": 2, "items": []}})
    assert preferences.get_nav_layout() is None
    with pytest.raises(ValueError, match="版本"):
        preferences.set_nav_layout(layout(), [])
    assert preferences.load()["nav_layout"]["v"] == 2


def test_endpoint_validation_and_save():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.settings import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.put("/api/settings/preferences/nav-layout", json={"nav_layout": layout(), "nav_hidden": ["/"]})
        assert response.status_code == 200
        assert response.json()["nav_layout"] == layout()
        assert response.json()["nav_hidden"] == []
        bad = client.put("/api/settings/preferences/nav-layout", json={"nav_layout": {"v": 3, "items": []}})
        assert bad.status_code == 400
        assert preferences.get_nav_layout() == layout()
