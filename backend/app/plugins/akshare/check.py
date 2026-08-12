"""Runtime readiness check for the optional AKShare provider."""
from __future__ import annotations

import importlib.util


def availability() -> tuple[bool, str]:
    if importlib.util.find_spec("akshare") is None:
        return False, "未安装 akshare Python 包"
    return True, "ok"
