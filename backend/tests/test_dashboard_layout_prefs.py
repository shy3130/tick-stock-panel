"""看板自定义布局偏好 — 保存往返、护栏与清除回默认。"""
from __future__ import annotations

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def _blob(**overrides) -> dict:
    item = {"i": "indices", "t": "indices", "x": 0, "y": 0, "w": 12, "h": 2}
    item.update(overrides)
    return {"v": 1, "items": [item]}


def test_layout_round_trip_and_get_default_none():
    assert preferences.get_dashboard_layout() is None

    saved = preferences.set_dashboard_layout(_blob())
    assert saved == _blob()
    assert preferences.get_dashboard_layout() == _blob()


def test_layout_null_clears_back_to_default():
    preferences.set_dashboard_layout(_blob())
    assert preferences.set_dashboard_layout(None) is None
    assert preferences.get_dashboard_layout() is None


def test_layout_clamps_and_normalizes_fields():
    saved = preferences.set_dashboard_layout({
        "v": 1,
        "items": [
            {"i": "x" * 100, "t": "radar", "x": 99, "y": -5, "w": 99, "h": 0, "p": {"url": "javascript:alert(1)"}},
            {"i": "", "t": ""},
            "not-a-dict",
        ],
    })
    # 后端只做护栏(类型/范围), 语义校验(未知组件/危险 URL)在前端规范化层
    (item,) = saved["items"]
    assert item["i"] == "x" * 64
    assert item["x"] == 11 and item["y"] == 0 and item["w"] == 12 and item["h"] == 1
    # 无 i/t 的项与纯垃圾项被剔除, 仍剩 1 条有效项
    assert saved is not None and len(saved["items"]) == 1


def test_layout_rejects_empty_items():
    with pytest.raises(ValueError):
        preferences.set_dashboard_layout({"v": 1, "items": []})
    with pytest.raises(ValueError):
        preferences.set_dashboard_layout({"v": 1, "items": ["junk", 42]})
    with pytest.raises(ValueError):
        preferences.set_dashboard_layout({"v": 1})


def test_layout_rejects_oversized_blob():
    # 单项字段有长度护栏, 用大量 p 键把 blob 顶过 64KB
    items = [{"i": f"ext-link-{k}", "t": "ext-link", "x": 0, "y": k, "w": 4, "h": 5,
              "p": {f"key{j}": "v" * 480 for j in range(4)}} for k in range(40)]
    with pytest.raises(ValueError):
        preferences.set_dashboard_layout({"v": 1, "items": items})


def test_settings_endpoint_round_trip(monkeypatch):
    from fastapi import HTTPException

    from app.api import settings as settings_api

    saved = settings_api.update_dashboard_layout(settings_api.DashboardLayoutIn(layout=_blob()))
    assert saved["dashboard_layout"] == _blob()

    cleared = settings_api.update_dashboard_layout(settings_api.DashboardLayoutIn(layout=None))
    assert cleared["dashboard_layout"] is None

    with pytest.raises(HTTPException):
        settings_api.update_dashboard_layout(settings_api.DashboardLayoutIn(layout={"v": 1, "items": []}))
