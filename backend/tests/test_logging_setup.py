"""app/logfile.py: 日志归拢 data/logs/ + 大小切割 + 旧位置一次性迁移。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import pytest

from app.logfile import attach_rotating_log
from app.config import settings


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    return tmp_path


@pytest.fixture()
def no_leaked_handlers():
    """测试后摘掉本用例新挂的 root handler, 并 close 释放文件句柄。

    RotatingFileHandler 构造即打开日志文件, 不 close 的话 Windows 上
    tmp_path 清理会因文件占用失败, 且 handler 会泄漏到后续用例。
    """
    before = {id(h) for h in logging.getLogger().handlers}
    yield
    for handler in list(logging.getLogger().handlers):
        if id(handler) not in before:
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_attaches_rotating_handler_and_migrates_legacy(data_dir, no_leaked_handlers):
    # 旧位置: data 根下的 backend.log 与轮转副本 .1
    (data_dir / "backend.log").write_text("old-main", encoding="utf-8")
    (data_dir / "backend.log.1").write_text("old-rot", encoding="utf-8")

    attach_rotating_log("backend.log", max_mb=10, backup_count=3)

    # 旧文件被搬进 logs/, 内容不丢
    assert (data_dir / "logs" / "backend.log").read_text(encoding="utf-8") == "old-main"
    assert (data_dir / "logs" / "backend.log.1").read_text(encoding="utf-8") == "old-rot"
    assert not (data_dir / "backend.log").exists()
    assert not (data_dir / "backend.log.1").exists()
    # handler 为轮转式, 参数符合调用
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert handlers
    added = handlers[-1]
    assert added.maxBytes == 10 * 1024 * 1024
    assert added.backupCount == 3


def test_no_legacy_files_starts_fresh(data_dir, no_leaked_handlers):
    attach_rotating_log("desktop.log", max_mb=10, backup_count=5)
    # RotatingFileHandler 构造即以追加模式打开目标文件
    assert (data_dir / "logs" / "desktop.log").exists()
    assert not (data_dir / "desktop.log").exists()


def test_existing_target_not_overwritten(data_dir, no_leaked_handlers):
    # logs/ 里已有同名文件 (如上一轮迁移过) → 旧位置的文件原地保留, 不覆盖
    (data_dir / "logs").mkdir()
    (data_dir / "logs" / "backend.log").write_text("new", encoding="utf-8")
    (data_dir / "backend.log").write_text("old", encoding="utf-8")

    attach_rotating_log("backend.log", max_mb=10, backup_count=3)

    assert (data_dir / "logs" / "backend.log").read_text(encoding="utf-8") == "new"
    assert (data_dir / "backend.log").read_text(encoding="utf-8") == "old"


def test_unwritable_data_dir_only_warns(tmp_path, monkeypatch, caplog, no_leaked_handlers):
    # data_dir 指向一个普通文件 → mkdir 失败 → 只 warning, 不抛异常、不加 handler
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "data_dir", blocker, raising=False)

    before_count = len(logging.getLogger().handlers)
    with caplog.at_level(logging.WARNING):
        attach_rotating_log("backend.log", max_mb=10, backup_count=3)

    assert "文件日志初始化失败" in caplog.text
    assert len(logging.getLogger().handlers) == before_count


# ── Windows 占用韧性: 滚动降级与停用 (用户反馈终端被 WinError 32 刷屏) ──


def test_rollover_degrades_to_copy_and_truncate(data_dir, no_leaked_handlers, monkeypatch):
    """rename 被占用 (WinError 32 场景) → copy-and-truncate 兜底, 滚动仍完成。"""
    import app.logfile as logfile

    attach_rotating_log("backend.log", max_mb=1, backup_count=1)
    handler = [h for h in logging.getLogger().handlers
               if isinstance(h, logfile.StallSafeRotatingFileHandler)][-1]
    handler.maxBytes = 60  # 小阈值便于触发滚动

    def boom(src, dst):
        raise OSError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(logfile.os, "rename", boom)
    log = logging.getLogger("t.rotate")
    for i in range(10):
        log.warning("line-%02d-%s", i, "x" * 20)

    # 滚动完成: .1 存在且含旧内容; 原文件被清空后继续写新内容
    rotated = (data_dir / "logs" / "backend.log.1").read_text(encoding="utf-8")
    assert rotated
    active = (data_dir / "logs" / "backend.log").read_text(encoding="utf-8")
    assert active and rotated != active
    assert rotated.startswith("202") or "line-" in rotated  # 内容确为日志行
    # handler 仍挂在 root 上继续工作 (未触发停用)
    assert handler in logging.getLogger().handlers


def test_persistent_rotate_failure_disables_handler(data_dir, no_leaked_handlers, monkeypatch, caplog):
    """滚动彻底失败 (copy-and-truncate 也炸) → 停用文件 handler + 单条 ERROR 指引,
    后续日志不再逐条刷堆栈。"""
    import app.logfile as logfile

    attach_rotating_log("backend.log", max_mb=1, backup_count=1)
    handler = [h for h in logging.getLogger().handlers
               if isinstance(h, logfile.StallSafeRotatingFileHandler)][-1]
    handler.maxBytes = 60
    # gh-116263 (Python 3.12.9+): 空文件永不滚动 — 预置内容让首条日志即触发滚动,
    # 否则 3.12 上 shouldRollover 直接 False, rotate/handleError 根本不会执行
    handler.stream.write("-" * 100 + "\n")
    handler.stream.flush()

    def boom(src, dest):
        raise OSError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(handler, "rotate", boom)
    log = logging.getLogger("t.stall")

    with caplog.at_level(logging.WARNING, logger="app.logfile"):
        log.warning("first-%s", "x" * 80)

    text = caplog.text
    assert "文件日志滚动失败" in text
    assert "文件日志已停用" in text
    # 已从 root 摘除; 后续日志静默通过 (终端 StreamHandler 不受影响), 不再新增告警
    assert handler not in logging.getLogger().handlers
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.logfile"):
        for i in range(10):
            log.warning("after-%02d-%s", i, "y" * 80)
    assert "文件日志" not in caplog.text
    assert caplog.text.count("Traceback") == 0
