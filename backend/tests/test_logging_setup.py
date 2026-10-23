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
