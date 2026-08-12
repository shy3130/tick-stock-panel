"""系统通知适配器 — 三平台原生通知中心。

职责: 把后端产生的告警事件推送到操作系统通知中心。
     窗口最小化 / 被遮挡 / 后台运行时都能弹通知 (不依赖前端 WebView)。

平台实现:
  - Windows: winotify (进现代操作中心, 支持图标)
  - macOS:   osascript (系统已内置, 无需额外依赖)
  - Linux:   notify-send (系统已内置) / plyer 兜底

设计: 失败静默降级, 绝不因通知失败阻断告警主流程 (落盘 / SSE 推送)。
     通知去重不在本层做, 复用 MonitorRuleEngine 的 cooldown 逻辑。
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 单次通知最长字符 (避免某些平台截断报错)
_MAX_LEN = 200

# 避免重复探测平台能力, 缓存一次
_backend_cache: str | None = None


def _detect_backend() -> str | None:
    """探测当前平台可用的通知后端。"""
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache if _backend_cache != "none" else None

    backend = None
    if sys.platform == "win32":
        try:
            import winotify  # type: ignore[import-not-found]  # noqa: F401

            backend = "winotify"
        except ImportError:
            logger.debug("winotify 不可用, Windows 通知降级")
            backend = None
    elif sys.platform == "darwin":
        backend = "osascript"
    elif sys.platform.startswith("linux"):
        backend = "notify-send"

    _backend_cache = backend or "none"
    return backend


def _truncate(text: str) -> str:
    """截断超长文本, 避免平台通知上限报错。"""
    text = (text or "").strip()
    return text[:_MAX_LEN] + ("…" if len(text) > _MAX_LEN else "")


def notify(title: str, message: str, icon: Path | None = None) -> bool:
    """推送一条系统通知。

    Args:
        title:   通知标题
        message: 通知正文
        icon:    可选图标路径 (部分平台支持)

    Returns:
        True=成功送达, False=失败或无可用后端。
        失败静默, 不抛异常 (通知是辅助通道, 不能阻断告警主流程)。
    """
    title = _truncate(title)
    message = _truncate(message)
    if not title:
        return False

    backend = _detect_backend()
    if backend is None:
        return False

    try:
        if backend == "winotify":
            return _notify_winotify(title, message)
        if backend == "osascript":
            return _notify_osascript(title, message)
        if backend == "notify-send":
            return _notify_notify_send(title, message, icon)
    except Exception as e:
        logger.debug("系统通知失败 (%s): %s", backend, e)
        return False

    return False


def _notify_winotify(title: str, message: str) -> bool:
    """Windows 通知 (winotify) — 进现代操作中心。"""
    from winotify import Notifier  # type: ignore[import-not-found]

    Notifier().create_notification(
        title=title,
        msg=message,
        # winotify 要求 duration 为 "short" 或 "long"
        duration="short",
        # 无可点击动作 (桌面版不实现"点击回到窗口"的复杂交互)
    ).show()
    return True


def _notify_osascript(title: str, message: str) -> bool:
    """macOS 通知 (osascript) — 调用系统 AppleScript。"""
    # Values travel in argv rather than being interpolated into AppleScript.
    # Quote/backslash escaping is otherwise easy to bypass with crafted alerts.
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    result = subprocess.run(
        ["osascript", "-e", script, "--", title, message],
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0


def _notify_notify_send(title: str, message: str, icon: Path | None) -> bool:
    """Linux 通知 (notify-send) — freedesktop.org 标准。"""
    args = ["notify-send", title]
    if icon and Path(icon).exists():
        args.extend(["--icon", str(icon)])
    args.append(message)
    result = subprocess.run(
        args,
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0
