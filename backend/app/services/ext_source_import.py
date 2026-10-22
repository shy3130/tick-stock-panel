"""源码扩展(.tsx)导入 — 宿主侧 esbuild 编译为自包含页面插件产物。

两个契约的鸿沟由生成的 shim 入口弥合:
- 源码扩展 (FrontendExtensionModule) 导出 React 组件路由; 页面插件
  (PagePackExtension) 是命令式 mount(el, ctx)。shim 用 createRoot 渲染组件。
- 打包后 react-query / react-router 是独立模块实例, 看不见宿主的
  Provider/Router — shim 自挂 QueryClientProvider + MemoryRouter。
- navigation.icon 是 lucide 组件, 插件契约要求白名单字符串名 — 按
  displayName 精确匹配 runtime.tsx ICON_ALLOWLIST, 兜底 "Sparkles"。

仅源码/桌面部署可发起导入 (需要 frontend/node_modules 里的 esbuild);
产物是标准页面插件, 全部署形态可用。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from app.config import settings

# 与 frontend/src/extensions/runtime.tsx ICON_ALLOWLIST 的键逐字一致 (大小写敏感)
_ICON_ALLOWLIST = [
    "Activity", "BarChart3", "Bell", "BookOpen", "Brain", "Calendar",
    "Database", "Globe", "LineChart", "PieChart", "Shield", "Sparkles",
    "Star", "Table", "TrendingUp", "Zap",
]

_NS_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

# 与 ext_pages._MAX_BUNDLE_BYTES 同限: 编译产物也不能超上传上限
_MAX_BUNDLE_BYTES = 5 * 1024 * 1024
_COMPILE_TIMEOUT_S = 60.0

_SHIM_TEMPLATE = """\
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ext from __ENTRY__

const qc = new QueryClient()
const ICON_ALLOWLIST = new Set(__ICONS__)
function iconName(icon: unknown): string {
  const name = String((icon as { displayName?: string } | null)?.displayName ?? '')
  return ICON_ALLOWLIST.has(name) ? name : 'Sparkles'
}
const packId = String(ext.id).includes('.') ? String(ext.id) : `imported.${ext.id}`
export default {
  id: packId,
  apiVersion: 1,
  routes: (ext.routes ?? []).map((r: { id: string; path: string; component: unknown }) => ({
    id: r.id,
    path: r.path,
    mount(el: HTMLElement) {
      const root = createRoot(el)
      root.render(
        createElement(QueryClientProvider, { client: qc },
          createElement(MemoryRouter, null, createElement(r.component as never))),
      )
      return () => root.unmount()
    },
  })),
  navigation: (ext.navigation ?? []).map((n: { id: string; routeId: string; label: string; icon?: unknown }) => ({
    id: n.id,
    routeId: n.routeId,
    label: n.label,
    icon: iconName(n.icon),
  })),
}
"""


class SourceImportUnavailableError(Exception):
    """当前部署没有 Node/esbuild 工具链 (如 Docker 产物部署)。"""


class SourceCompileError(Exception):
    """esbuild 编译失败 (语法错误 / 无法解析的 import 等)。"""


def frontend_root() -> Path:
    """前端根目录 (源码部署 = <项目根>/frontend)。测试可 monkeypatch。"""
    return settings.static_dir.parent


def custom_root() -> Path:
    return frontend_root() / "src" / "custom"


def find_esbuild() -> Path | None:
    """按常见包管理器布局发现 esbuild 二进制; 找不到再退回 PATH。

    pnpm 布局下 esbuild 是 vite 的传递依赖, 不出现在 .bin —— 必须扫
    .pnpm/@esbuild+<ver>/node_modules/@esbuild/<platform>/bin/esbuild。
    """
    root = frontend_root()
    candidates: list[Path] = [root / "node_modules" / ".bin" / "esbuild"]
    candidates.extend(sorted((root / "node_modules" / ".pnpm").glob(
        "@esbuild+*/node_modules/@esbuild/*/bin/esbuild")))
    candidates.extend(sorted((root / "node_modules" / "@esbuild").glob(
        "*/bin/esbuild")))
    for c in candidates:
        if c.is_file():
            return c
    from shutil import which
    exe = which("esbuild")
    return Path(exe) if exe else None


def namespace_source_path(namespace: str) -> Path:
    """frontend/src/custom/<ns>/extension.tsx, 带命名空间与穿越校验。"""
    if not _NS_RE.match(namespace) or namespace == "_template":
        raise ValueError(f"非法的扩展命名空间: {namespace}")
    p = (custom_root() / namespace / "extension.tsx").resolve()
    if p.parent.parent != custom_root().resolve():
        raise ValueError("非法路径")
    if not p.is_file():
        raise ValueError(f"扩展命名空间不存在: {namespace}")
    return p


def list_namespaces() -> list[str]:
    """可导入的本项目源码扩展清单 (有 extension.tsx 的目录)。"""
    root = custom_root()
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and (d / "extension.tsx").is_file()
    )


def compile_source(source_path: Path, pack_id_hint: str) -> tuple[bytes, str]:
    """esbuild 编译单个 .tsx 扩展为自包含页面插件产物。

    编译临时目录放在 frontend/node_modules/.tsp-import-*/ 下: esbuild 按文件
    位置向上找 node_modules, 系统临时目录解析不到裸 import (react 等);
    node_modules 也是构建产物的归属地, Vite 不监视它。本项目内的源码保持
    原位编译 (保留其相对 import), 外部上传的源文件拷入临时目录。

    Returns: (bundle_bytes, entry_summary) — entry_summary 用于日志/错误信息。
    Raises: SourceImportUnavailableError / SourceCompileError。
    """
    esbuild = find_esbuild()
    if esbuild is None:
        raise SourceImportUnavailableError(
            "当前部署没有 esbuild 工具链, 源码导入仅源码/桌面部署可用")

    root = frontend_root()
    try:
        source = source_path.resolve()
    except OSError as e:
        raise SourceCompileError(f"源文件不可读: {e}") from e
    under_frontend = root.resolve() in source.parents

    tmp = root / "node_modules" / f".tsp-import-{uuid.uuid4().hex[:8]}"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        if not under_frontend:
            copied = tmp / "source.tsx"
            copied.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            entry_source = copied
        else:
            entry_source = source
        shim = tmp / "shim.tsx"
        shim.write_text(
            _SHIM_TEMPLATE
            .replace("__ENTRY__", json.dumps(str(entry_source)))
            .replace("__ICONS__", json.dumps(_ICON_ALLOWLIST)),
            encoding="utf-8",
        )
        out = tmp / "bundle.js"
        # 参数全部内部构造, esbuild 二进制来自本仓库 frontend/node_modules
        proc = subprocess.run(
            [
                str(esbuild), str(shim),
                "--bundle", "--format=esm", "--minify",
                "--jsx=automatic",
                f"--outfile={out}",
                f"--alias:@={root / 'src'}",
                "--define:process.env.NODE_ENV=\"production\"",
                "--loader:.css=empty",
                "--log-level=error",
            ],
            capture_output=True,
            text=True,
            timeout=_COMPILE_TIMEOUT_S,
        )
        if proc.returncode != 0 or not out.is_file():
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise SourceCompileError(f"esbuild 编译失败: {tail or '无输出'}")
        data = out.read_bytes()
        if len(data) > _MAX_BUNDLE_BYTES:
            raise SourceCompileError(
                f"编译产物 {len(data) // 1024}KB 超过 {_MAX_BUNDLE_BYTES // (1024 * 1024)}MB 上限")
        return data, f"{source_path.name} ({len(data) // 1024}KB)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
