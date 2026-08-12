"""Runtime readiness check for the optional Tushare provider."""
from __future__ import annotations

import importlib.util
import os


def availability() -> tuple[bool, str]:
    if importlib.util.find_spec("tushare") is None:
        return False, "未安装 tushare Python 包"
    if not os.getenv("TUSHARE_TOKEN"):
        return False, "TUSHARE_TOKEN 未配置"
    return True, "ok"
