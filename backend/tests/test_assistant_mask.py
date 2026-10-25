"""AI 助手毛玻璃遮罩开关 — 助手打开时页面盖遮罩, 点击遮罩关闭助手。

纯前端视觉/交互偏好的持久化契约: 默认开启 (聚焦提问), 可在系统设置 → 界面
关闭以恢复旧版非模态体验 (边看行情边问)。这里锁住: 默认值、读写回环、
独立保存不覆盖其它偏好、GET /preferences 可见。
"""
from __future__ import annotations

import pytest

from app.services import preferences


@pytest.fixture()
def prefs_file(tmp_path, monkeypatch):
    """把 preferences.json 指到 tmp, 并清掉进程内缓存。"""
    from pathlib import Path

    target = Path(tmp_path) / "user_data" / "preferences.json"
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(preferences, "_path", lambda: target)
    preferences._invalidate_cache()
    yield target
    preferences._invalidate_cache()


def test_mask_defaults_on(prefs_file):
    """未设置过 → True (默认开启毛玻璃遮罩)。"""
    assert preferences.get_assistant_mask_enabled() is True


def test_mask_roundtrip(prefs_file):
    assert preferences.set_assistant_mask_enabled(False) is False
    assert preferences.get_assistant_mask_enabled() is False
    assert preferences.set_assistant_mask_enabled(True) is True
    assert preferences.get_assistant_mask_enabled() is True


def test_mask_save_preserves_other_prefs(prefs_file):
    """独立保存不清掉其它偏好 (设置页各控件独立保存, 走 merge-write)。"""
    preferences.save({"screener_auto_run": False})
    preferences.set_assistant_mask_enabled(False)
    data = preferences.load()
    assert data["screener_auto_run"] is False
    assert data["assistant_mask_enabled"] is False


def test_get_preferences_exposes_mask(prefs_file):
    """GET /api/settings/preferences 必须带 assistant_mask_enabled — 前端抽屉渲染依赖。"""
    from app.api.settings import get_preferences

    preferences.set_assistant_mask_enabled(False)
    payload = get_preferences()
    assert payload["assistant_mask_enabled"] is False


def test_put_endpoint_persists_switch(prefs_file):
    """PUT /preferences/assistant-mask: 落盘并回显 (直接调端点函数, 不起 app)。"""
    from app.api.settings import AssistantMaskIn, update_assistant_mask

    result = update_assistant_mask(AssistantMaskIn(enabled=False))
    assert result == {"assistant_mask_enabled": False}
    assert preferences.get_assistant_mask_enabled() is False
