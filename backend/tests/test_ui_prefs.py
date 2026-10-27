"""界面偏好 (ui_prefs) 跨端持久化 — 原先只存 localStorage 的设置改由后端保存副本。

背景: 浏览器按 origin (协议+主机+端口) 隔离 localStorage。桌面版启动时用
_find_free_port(3018) 取第一个空闲端口并以 http://127.0.0.1:{port} 加载界面,
端口一变就是另一个 origin —— 通知弹窗/声效/语音播报/外链模板/界面缩放这些
只存 localStorage 的设置全部读不到, 表现为「设置保存了重开又变回去」。

这里锁住后端契约: 白名单、标量类型护栏、null 删除、体积上限、GET 可见。
"""
from __future__ import annotations

import pytest

from app.services import preferences


@pytest.fixture()
def prefs_file(tmp_path, monkeypatch):
    """把 preferences.json 指到 tmp, 并清掉进程内缓存。

    不动 app.config.settings 对象本身 (其他模块在 import 时读它的属性),
    只替换 preferences._path 这一条路径计算。
    """
    from pathlib import Path

    target = Path(tmp_path) / "user_data" / "preferences.json"
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(preferences, "_path", lambda: target)
    preferences._invalidate_cache()
    yield target
    preferences._invalidate_cache()


def test_defaults_empty(prefs_file):
    """从未设置过 → 空对象 (前端用自己的默认值, 后端不伪造)。"""
    assert preferences.get_ui_prefs() == {}


def test_set_and_get_roundtrip(prefs_file):
    saved = preferences.set_ui_prefs({
        "alert_toast_max": 5,
        "voice_broadcast_enabled": True,
        "page_zoom": 1.15,
        "stock_external_template": "https://x/{code}",
    })
    assert saved["alert_toast_max"] == 5
    assert saved["voice_broadcast_enabled"] is True
    assert saved["page_zoom"] == 1.15
    assert preferences.get_ui_prefs()["stock_external_template"] == "https://x/{code}"


def test_unknown_keys_ignored(prefs_file):
    """未知键不进白名单 — 该 blob 由前端自由读写, 不能成为写任意结构的入口。"""
    saved = preferences.set_ui_prefs({"evil": {"a": 1}, "alert_toast_max": 4})
    assert "evil" not in saved
    assert saved == {"alert_toast_max": 4}


def test_non_scalar_rejected(prefs_file):
    with pytest.raises(ValueError, match="只接受标量"):
        preferences.set_ui_prefs({"alert_toast_max": [1, 2]})


def test_null_removes_item(prefs_file):
    """null = 删除该项, 回前端默认 (而不是存一个 null)。"""
    preferences.set_ui_prefs({"alert_sound": "chime", "alert_toast_max": 3})
    saved = preferences.set_ui_prefs({"alert_sound": None})
    assert "alert_sound" not in saved
    assert saved["alert_toast_max"] == 3


def test_merge_preserves_other_keys(prefs_file):
    """增量更新不清掉之前写入的其它项 (设置页各控件独立保存)。"""
    preferences.set_ui_prefs({"alert_toast_enabled": False})
    preferences.set_ui_prefs({"page_zoom": 0.9})
    got = preferences.get_ui_prefs()
    assert got == {"alert_toast_enabled": False, "page_zoom": 0.9}


def test_oversize_rejected(prefs_file):
    with pytest.raises(ValueError, match="过大"):
        preferences.set_ui_prefs({"stock_external_template": "x" * (17 * 1024)})


def test_get_preferences_exposes_ui_prefs(prefs_file):
    """GET /api/settings/preferences 必须带 ui_prefs — 前端启动回填依赖它。"""
    from app.api.settings import get_preferences

    preferences.set_ui_prefs({"alert_toast_max": 2})
    payload = get_preferences()
    assert payload["ui_prefs"] == {"alert_toast_max": 2}


def test_assistant_fab_enabled_in_whitelist(prefs_file):
    """AI 助手悬浮球显隐必须在白名单内 — 否则桌面版换端口 (换 origin) 后
    localStorage 丢失, 开关闪回默认开启 (用户反馈「关了过一会又开」)。"""
    saved = preferences.set_ui_prefs({"assistant_fab_enabled": False})
    assert saved["assistant_fab_enabled"] is False
    assert preferences.get_ui_prefs()["assistant_fab_enabled"] is False
