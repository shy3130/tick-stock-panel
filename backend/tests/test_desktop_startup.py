"""desktop.py 启动失败路径测试。

复现的原始问题 (2026-10-07): panel 模式/残留后端占住 data_dir/.mining_process.lock,
桌面版 lifespan 抢锁失败被 uvicorn 吞掉, 主线程傻等 150s 后只显示笼统的
"启动超时", 真因不可见。修复 = 预检秒失败 + 等待循环感知后端线程退出 +
失败页透传原因。
"""
from __future__ import annotations

import threading
import time

import pytest

import app.desktop as desktop
from app.config import settings
from app.desktop import (
    _failed_html,
    _precheck_mining_lock,
    _wait_for_server,
)
from app.services.mining_process_lock import MiningProcessLock


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    return tmp_path


# ---------- _failed_html ----------

def test_failed_html_timeout_keeps_original_copy():
    html_text = _failed_html(None)
    assert "启动超时" in html_text
    assert "后端未在预期时间内就绪" in html_text


def test_failed_html_shows_reason_with_title_change():
    html_text = _failed_html("另一个后端实例正在占用数据目录")
    assert "启动失败" in html_text
    assert "另一个后端实例正在占用数据目录" in html_text


def test_failed_html_escapes_reason_text():
    html_text = _failed_html("<script>alert(1)</script>")
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text


def test_failed_html_renders_newlines():
    html_text = _failed_html("第一行\n第二行")
    assert "第一行<br>第二行" in html_text


# ---------- _wait_for_server ----------

def test_wait_for_server_returns_immediately_when_backend_thread_dead():
    """后端线程已退出 (done_event 置位) 时不得等满 timeout —— 把启动失败伪装成超时的根源。"""
    done = threading.Event()
    done.set()
    start = time.monotonic()
    assert _wait_for_server(port=59999, timeout=10.0, done_event=done) is False
    assert time.monotonic() - start < 1.0


def test_wait_for_server_times_out_when_alive_but_not_ready():
    """后端线程活着但 health 不通: 等满 timeout 返回 False (原行为不变)。"""
    done = threading.Event()
    start = time.monotonic()
    assert _wait_for_server(port=59999, timeout=0.6, done_event=done) is False
    assert time.monotonic() - start >= 0.5


# ---------- _precheck_mining_lock ----------

def test_precheck_mining_lock_passes_when_free(data_dir):
    assert _precheck_mining_lock() is None


def test_precheck_mining_lock_pass_releases_lock(data_dir):
    """预检通过后必须释放 —— 否则预检自己挡死 lifespan。"""
    assert _precheck_mining_lock() is None
    holder = MiningProcessLock(data_dir)
    holder.acquire()  # 仍能抢到 = 预检确实释放了
    holder.release()


def test_precheck_mining_lock_detects_conflict(data_dir):
    holder = MiningProcessLock(data_dir)
    holder.acquire()
    reason = _precheck_mining_lock()
    assert reason is not None
    # 失败页要给出可操作的下一步, 不是一句"无法启动"
    assert "结束残留的 python" in reason
    holder.release()
    assert _precheck_mining_lock() is None


# ---------- main() 预检冲突路径 ----------

def test_main_proceeds_when_mining_lock_precheck_conflicts(
    tmp_path, monkeypatch, caplog
):
    """预检撞锁只降级: 留 WARNING 日志并继续启动, 不再提前退出失败页。

    lifespan 会以降级模式起面板 (挖掘入口 409, 挖掘页提供强制释放),
    桌面版必须照常开窗, 否则用户没有入口处理锁冲突。
    """
    import logging

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(desktop, "_guard_streams", lambda: None)
    monkeypatch.setattr(desktop, "_setup_logging", lambda: None)
    monkeypatch.setattr(
        desktop, "_precheck_mining_lock", lambda: "另一个后端实例正在占用数据目录"
    )
    monkeypatch.setattr(desktop, "_find_free_port", lambda base: 59998)
    monkeypatch.setattr(
        desktop, "_wait_for_server",
        lambda port, timeout, done_event: True,
    )
    opened = []
    monkeypatch.setattr(desktop, "_open_window", lambda url: opened.append(url))

    def _fake_uvmicorn(port, ready):
        ready.set()

    monkeypatch.setattr(desktop, "_run_uvmicorn", _fake_uvmicorn)

    with caplog.at_level(logging.WARNING, logger="app.desktop"):
        assert desktop.main() == 0

    assert opened and opened[0].startswith("http://127.0.0.1:")
    assert "启动预检" in caplog.text
    assert not (tmp_path / ".desktop.lock").exists()


# ---------- _open_window: pywebview 下载启用 ----------

def test_open_window_enables_downloads_before_start(monkeypatch):
    """pywebview 5.0+ 默认取消所有文件下载 (Edge WebView2 / WKWebView 一致) —
    必须在 webview.start() 前显式开启 ALLOW_DOWNLOADS, 否则配置备份导出、
    回测 CSV 等 <a download> 直链在桌面版点击无反应 (2026-10-27 用户反馈)。"""
    import sys
    import types

    calls: list[str] = []

    class _RecordingSettings(dict):
        def __setitem__(self, key, value):
            calls.append(f"set:{key}={value}")
            super().__setitem__(key, value)

    fake = types.ModuleType("webview")
    fake.settings = _RecordingSettings()
    fake.create_window = lambda *a, **k: calls.append("create_window") or object()
    fake.start = lambda *a, **k: calls.append("start")
    monkeypatch.setitem(sys.modules, "webview", fake)

    desktop._open_window("http://127.0.0.1:1")

    assert fake.settings.get("ALLOW_DOWNLOADS") is True
    assert calls == ["set:ALLOW_DOWNLOADS=True", "create_window", "start"]
