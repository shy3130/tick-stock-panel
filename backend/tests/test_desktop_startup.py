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

def test_main_releases_lock_when_failure_window_crashes(
    tmp_path, monkeypatch, caplog
):
    """预检冲突 + 失败页窗口自身崩溃 (如缺 webview 依赖): 异常不得裸穿 main()。

    该路径在 main() 的 try/finally 之外, 不兜底会导致 console=False 闪退无反馈、
    .desktop.lock 跳过释放。断言: 记录异常日志、单实例锁仍释放、退出码 1。
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(desktop, "_guard_streams", lambda: None)
    monkeypatch.setattr(desktop, "_setup_logging", lambda: None)
    monkeypatch.setattr(desktop, "_precheck_mining_lock", lambda: "冲突原因")

    def _boom(_html_text):
        raise RuntimeError("webview unavailable")

    monkeypatch.setattr(desktop, "_open_static_window", _boom)

    import logging

    with caplog.at_level(logging.ERROR, logger="app.desktop"):
        assert desktop.main() == 1

    assert "启动失败页窗口打开失败" in caplog.text
    assert not (tmp_path / ".desktop.lock").exists()
