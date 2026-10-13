"""Tick Stock Panel backend."""

import json
import sys
from pathlib import Path

# 版本号唯一权威 = frontend/package.json: 安装包 AppVersion (release.yml 读它传给
# ISCC)、macOS 包元数据 (tsp.spec 读它)、页面版本徽标与更新检查 (本模块) 全部同源,
# 改版本只改那一份文件。dev 模式读仓库文件; PyInstaller 冻结后读 _MEIPASS/package.json
# (tsp.spec 的 datas 会收集)。读取失败回退硬编码值 —— 版本号绝不阻塞应用启动。
_FALLBACK_VERSION = "0.3.6"


def _load_version() -> str:
    if getattr(sys, "frozen", False):
        pkg = Path(getattr(sys, "_MEIPASS", ".")) / "package.json"
    else:
        # backend/app/__init__.py 上溯两级 = 项目根
        pkg = Path(__file__).resolve().parents[2] / "frontend" / "package.json"
    try:
        return json.loads(pkg.read_text(encoding="utf-8"))["version"]
    except Exception:
        return _FALLBACK_VERSION


__version__ = _load_version()

# Windows 默认 stdout/stderr 编码为 GBK(cp936),TickFlow SDK 内部输出含 emoji 的
# 指数/标的名称(如 \U0001f193)时会抛 UnicodeEncodeError,导致请求失败。
# 进程加载最早阶段强制 UTF-8,根治此类编码崩溃。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
