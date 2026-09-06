"""复盘推送触发方式 review_push_mode 测试 — auto/manual 白名单与默认值。"""
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


def test_review_push_mode_defaults_to_manual():
    assert preferences.get_review_push_mode() == "manual"


def test_set_and_get_review_push_mode():
    assert preferences.set_review_push_mode("auto") == "auto"
    assert preferences.get_review_push_mode() == "auto"

    assert preferences.set_review_push_mode("manual") == "manual"
    assert preferences.get_review_push_mode() == "manual"


def test_set_review_push_mode_rejects_invalid_value():
    assert preferences.set_review_push_mode("bogus") == "manual"
    assert preferences.get_review_push_mode() == "manual"
