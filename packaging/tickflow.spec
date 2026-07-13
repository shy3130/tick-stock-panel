# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — 桌面客户端 (onedir 模式)。

为什么 onedir 而非 onefile:
  - onefile 每次启动都解压到临时 _MEIxxxxx, 与 APScheduler/多线程冲突
  - onedir 启动更快, 调试更方便 (可看到目录结构), 原生库直接在目录里
  - 体积差异通过压缩安装包弥补 (CI 里 zip 打包)

入口: backend/app/desktop.py (桌面版入口, 含 uvicorn + pywebview)

构建 (在项目根目录):
  cd frontend && pnpm build                     # 先构建前端到 frontend/dist
  pyinstaller packaging/tickflow.spec           # 产物在 dist/TickFlowStockPanel/
"""
import sys
from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_submodules,
    collect_data_files,
    copy_metadata,
)

_IS_MACOS = sys.platform == "darwin"

block_cipher = None

# ── 资源路径基准: 项目根 (spec 文件在 packaging/ 下) ──────────────────
ROOT = Path(SPECPATH).parent
FRONTEND_DIST = str(ROOT / "frontend" / "dist")
TIERS_YAML = str(ROOT / "tiers.yaml")
BUILTIN_STRATEGIES = str(ROOT / "backend" / "app" / "strategy" / "builtin")
# 图标按平台选: Windows 用 .ico, macOS 用 .icns (PyInstaller 对 .ico 在
# mac 上静默忽略, 不换格式 Dock/Finder 会显示通用图标)。两者都由
# packaging/generate_icon.py 一并生成。
APP_ICON = str(ROOT / "packaging" / ("icon.icns" if _IS_MACOS else "icon.ico"))

# ── 收集带原生库的依赖 (.libs/ 目录必须完整, 否则启动崩) ─────────────
# polars / pyarrow / duckdb / fastexcel 都自带共享库子目录
datas = []
binaries = []
hiddenimports = []

for pkg in ("polars", "pyarrow", "duckdb", "fastexcel"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Polars 的发行包名为 polars-runtime-32 / polars-runtime-compat, 但实际
# Python 导入包带前导下划线。release.yml 安装 legacy-cpu 后必须收集二者，
# 否则 onedir 产物无法在没有 AVX2/FMA 的旧 CPU 上加载兼容内核。
for pkg in ("_polars_runtime_32", "_polars_runtime_compat"):
    if find_spec(pkg) is not None:
        rt_d, rt_b, rt_h = collect_all(pkg)
        datas += rt_d
        binaries += rt_b
        hiddenimports += rt_h

# Polars 新 ABI 运行时由加载器选择，需显式收集子模块。
hiddenimports += collect_submodules("polars")

# ── pywebview 平台后端 (动态导入, PyInstaller 默认抓不到) ────────────
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("webview.platforms")

# ── 系统通知后端 (winotify/plyer 按平台动态导入) ─────────────────────
if sys.platform == "win32":
    hiddenimports += collect_submodules("winotify")
hiddenimports += collect_submodules("plyer")
hiddenimports += collect_submodules("plyer.platforms")

# ── uvicorn 动态导入的模块 (loop/protocol/logging 按字符串加载) ──────
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

# ── fastapi / pydantic 元数据 (版本检测用) ───────────────────────────
# 注意: 任何用 importlib.metadata.version() 读版本的包, 都必须 copy_metadata,
# 否则 frozen 后报 PackageNotFoundError。tickflow 包内部就是这么读的。
# 用容错写法: 不存在的包跳过, 避免不同环境 (有无装某依赖) 导致构建失败。
def _safe_metadata(pkg):
    """收集包元数据, 包不存在时静默跳过。"""
    try:
        return copy_metadata(pkg)
    except Exception:
        return []

for pkg in (
    "fastapi", "pydantic", "pydantic_settings", "starlette", "anyio",
    "tickflow",  # tickflow/__version__.py 用 importlib.metadata 读版本
    "uvicorn", "polars", "duckdb", "pyarrow", "httpx", "numpy", "pandas",
    "openai", "platformdirs", "winotify", "plyer", "apscheduler",
    "python-dotenv", "fastexcel",
):
    datas += _safe_metadata(pkg)

# ── 随包资源 (只读, 放进 _MEIPASS) ────────────────────────────────────
# 前端 dist → static/ (config.py frozen 模式读 _MEIPASS/static)
datas += [(FRONTEND_DIST, "static")]
# tiers.yaml → 包根 (config.py frozen 模式读 _MEIPASS/tiers.yaml)
datas += [(TIERS_YAML, ".")]
# 内置策略 → app/strategy/builtin/ (importlib 动态加载, 不能进 PYZ)
datas += [(BUILTIN_STRATEGIES, "app/strategy/builtin")]

# ── 排除不需要的重型依赖 (主包不含 vectorbt 回测链) ──────────────────
excludes = [
    "vectorbt",
    "numba",
    "llvmlite",
    "matplotlib",
    "plotly",
    "ipywidgets",
    "nbformat",
    "nbconvert",
    "jupyter",
    "IPython",
    "pytest",
    "pytest_asyncio",
    "ruff",
    "mypy",
]

a = Analysis(
    [str(ROOT / "backend" / "app" / "desktop.py")],
    pathex=[str(ROOT / "backend")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TickFlowStockPanel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 压缩原生库常导致崩溃, 关闭
    console=False,       # 桌面应用: 不显示控制台窗口 (_guard_streams 守护 stdout/stderr)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=APP_ICON,      # 应用图标 (与 favicon/logo 一致)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TickFlowStockPanel",
)

# ── macOS: 封装成 .app 包 ────────────────────────────────────────────
# Windows/Linux: 上面 COLLECT 产出的 onedir 目录即是最终产物。
# macOS: 额外加 BUNDLE, 把目录包装成标准 .app (Contents/MacOS/...),
# 这样 Dock/Finder 能识别, 用户可双击启动, 并显示自定义 .icns 图标。
# BUNDLE 必须引用上面的 COLLECT (coll), 它会把 coll 的产物搬进 .app。
#
# PyInstaller 6 BUNDLE 关键参数 (见 building/osx.py):
#   - version:            → CFBundleShortVersionString (默认 0.0.0, 必须显式传)
#   - info_plist (单数!):  用户自定义键, update 合并进默认 plist, 可覆盖任意字段
if _IS_MACOS:
    import json

    # 版本号从 frontend/package.json 读, 与 Release tag 对齐
    _pkg_json = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    APP_VERSION = _pkg_json["version"]

    app = BUNDLE(
        coll,
        name="TickFlowStockPanel.app",
        icon=APP_ICON,
        bundle_identifier="com.tickflow.stockpanel",
        version=APP_VERSION,   # → CFBundleShortVersionString / CFBundleVersion
        info_plist={
            "CFBundleName": "TickFlow Stock Panel",
            "CFBundleDisplayName": "TickFlow 股票面板",
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
        },
    )
