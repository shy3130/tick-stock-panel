"""TickFlow Stock Panel backend."""

import sys

__version__ = "0.1.68"

# Windows 默认 stdout/stderr 编码为 GBK(cp936),外部数据文本含 emoji 时
# 会抛 UnicodeEncodeError,导致请求失败。
# 进程加载最早阶段强制 UTF-8,根治此类编码崩溃。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
