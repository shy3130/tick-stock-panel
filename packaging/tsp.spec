# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — 桌面客户端 (onedir 模式)。

为什么 onedir 而非 onefile:
  - onefile 每次启动都解压到临时 _MEIxxxxx, 与 APScheduler/多线程冲突
  - onedir 启动更快, 调试更方便 (可看到目录结构), 原生库直接在目录里
  - 体积差异通过压缩安装包弥补 (CI 里 zip 打包)

入口: backend/app/desktop.py (桌面版入口, 含 uvicorn + pywebview)

构建 (在项目根目录):
  cd frontend && pnpm build                     # 先构建前端到 frontend/dist
  pyinstaller packaging/tsp.spec                # 产物在 dist/TSP/
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
RESEARCH_TEMPLATE = str(ROOT / "backend" / "app" / "strategy" / "research")
# 内置策略种子目录 (strategy_seeds 按路径读文本, frozen 下需落盘, 同 research)
# 图标按平台选: Windows 用 brand/icon.ico, macOS 用 brand/icon.icns —
# 两者均由 brand/png/logo-tile-light-512.png 派生 (浅色圆角卡版, 品牌资产唯一来源,
# 见 brand/README 接入指引), 无需 CI 现场生成。
APP_ICON = str((ROOT / "brand" / "icon.icns") if _IS_MACOS else (ROOT / "brand" / "icon.ico"))

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

# tdxaidata (TdxQuant 插件依赖) — 后端对其全部懒导入, 必须显式声明:
#   纯 Python 接口 (包 __init__ + tdxaidata.py) 进 PYZ; lib/ 是跨平台原生库
#   合集 (win dll / mac dylib / linux so 同包), 运行时由 client.materialize_lib
#   复制物化后 ctypes 加载, 属数据文件 — 只能整目录按 datas 复制。
#   注意不能用 collect_all / collect_submodules: lib/ 下的 libTdxAiData.so
#   会被当成 Python 扩展子模块收进 binaries, mac 构建的 macholib 解析
#   Linux ELF 直接崩。
if (spec_tdx := find_spec("tdxaidata")) is not None and spec_tdx.origin:
    _tdx_pkg = Path(spec_tdx.origin).parent
    datas += [(str(_tdx_pkg / "lib"), "tdxaidata/lib")]
    hiddenimports += ["tdxaidata", "tdxaidata.tdxaidata"]

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

# ── 内置可选插件 (app/plugins/*) ─────────────────────────────────────
# loader._load_builtin_plugins() 在文件系统扫 <_internal>/app/plugins/*/plugin.yaml,
# entry 又是 importlib 字符串动态导入 — 两条链路静态分析都看不见, 必须双声明:
# datas 把清单 yaml 落盘 (frozen 下 plugins_dir() 恰好解析到 _internal/app/plugins),
# hiddenimports 把 provider/client 塞进 PYZ。
# 注: 其他内置插件如需进桌面包, 仿照 fuyao 双声明添加。
datas += [
    (str(ROOT / "backend" / "app" / "plugins" / "fuyao" / "plugin.yaml"), "app/plugins/fuyao"),
    (str(ROOT / "backend" / "app" / "plugins" / "tdxquant" / "plugin.yaml"), "app/plugins/tdxquant"),
]
hiddenimports += [
    "app.plugins.fuyao.provider",
    "app.plugins.fuyao.client",
    "app.plugins.tdxquant.provider",
    "app.plugins.tdxquant.client",
    "app.plugins.tdxquant.probe",
]

# ── 后端源码扩展 (app/custom/*) ──────────────────────────────────────
# extensions/loader.py 运行时用 importlib.import_module + pkgutil.iter_modules
# 发现 app/custom/* — 与插件同样对静态分析不可见, 必须双声明 (issue #459:
# 未声明时冻结包里扩展静默消失, 助手路由未注册 → 请求落到 GET-only SPA 兜底
# 返回 405):
#   datas 落盘到 _internal/app/custom — frozen 下 app.__path__ 解析到
#     _internal/app, 保 pkgutil 枚举与磁盘可见性;
#   hiddenimports 进 PYZ — 保 importlib.import_module 可导入。
datas += [(str(ROOT / "backend" / "app" / "custom"), "app/custom")]
hiddenimports += collect_submodules("app.custom")

# ── pywebview 平台后端 (动态导入, PyInstaller 默认抓不到) ────────────
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("webview.platforms")
# pywebview 运行时依赖链 (webview/__init__ 顶层 import proxy_tools, Windows 端
# 还要 bottle/clr_loader/pythonnet)。显式声明为金丝雀: 构建环境缺这些包时
# PyInstaller 直接报错, 而不是静默产出启动即崩的包 (曾因 venv 缺 proxy_tools
# 发布过启动崩溃版本)。
hiddenimports += ["proxy_tools", "bottle", "clr_loader", "pythonnet"]

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
# frontend/package.json → 包根 (app/__init__.py frozen 模式读 _MEIPASS/package.json,
# __version__ 与安装包 AppVersion / 页面显示同源, 版本号只改 package.json 一处)
datas += [(str(ROOT / "frontend" / "package.json"), ".")]
# 研究模板 → app/strategy/research/ (importlib 动态加载, 不能进 PYZ; 挖掘功能依赖)
datas += [(RESEARCH_TEMPLATE, "app/strategy/research")]
# 策略种子 → app/strategy/seeds/ (启动时按路径读取种入 custom, 不进 PYZ)
datas += [(str(ROOT / "backend" / "app" / "strategy" / "seeds"), "app/strategy/seeds")]

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
    name="TSP",
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
    name="TSP",
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
        name="TSP.app",
        icon=APP_ICON,
        bundle_identifier="com.tickflow.stockpanel",
        version=APP_VERSION,   # → CFBundleShortVersionString / CFBundleVersion
        info_plist={
            "CFBundleName": "Tick Stock Panel",
            "CFBundleDisplayName": "TSP",
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
        },
    )
