"""版本号统一派生 — 从 frontend/package.json 同步三处伴生版本标记。

版本号唯一权威 = frontend/package.json (issue #513: 发版只改它时, VERSION /
backend/pyproject.toml / _FALLBACK_VERSION 三处伴生标记会漂移; Docker 部署
下运行时读不到 frontend/package.json, 实际生效的是 _FALLBACK_VERSION, 于是
界面/日志/version API 显示旧版本)。

用法:
    python3 scripts/sync_versions.py            # 幂等写回三处 (无变化则零写入)
    python3 scripts/sync_versions.py --check    # 只校验不一致并退出码 1 (CI 可选)

release.yml / docker.yml 在检出后调用本脚本, 保证构建产物内版本一致;
本地改完 package.json 后也可手动跑一次。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "frontend" / "package.json"
VERSION_FILE = ROOT / "VERSION"
PYPROJECT = ROOT / "backend" / "pyproject.toml"
APP_INIT = ROOT / "backend" / "app" / "__init__.py"


def pkg_version() -> str:
    return str(json.loads(PKG.read_text(encoding="utf-8"))["version"]).strip()


def _replace_once(text: str, pattern: str, repl: str, path: Path) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if n != 1:
        raise SystemExit(f"{path}: 未找到版本标记 (pattern={pattern!r})")
    return new


def sync(ver: str, *, check: bool = False) -> list[str]:
    """幂等写回三处伴生标记, 返回发生变化的文件 (相对仓库根)。"""
    # (路径, 匹配单行的正则, 写回的整行)
    targets = [
        (VERSION_FILE, r"^[^\n]*\n?", f"v{ver}\n"),
        (PYPROJECT, r'^version = "[^"]*"\n', f'version = "{ver}"\n'),
        (APP_INIT, r'^_FALLBACK_VERSION = "[^"]*"\n', f'_FALLBACK_VERSION = "{ver}"\n'),
    ]
    changed: list[str] = []
    for path, pattern, want_line in targets:
        old = path.read_text(encoding="utf-8")
        new = _replace_once(old, pattern, want_line, path)
        if new != old:
            changed.append(str(path.relative_to(ROOT)))
            if not check:
                path.write_text(new, encoding="utf-8")
    return changed


def main() -> int:
    # Windows runner 的 stdout/stderr 默认 cp1252, 中文输出直接 UnicodeEncodeError
    # (CI 里杀掉整个作业) — 统一重配为 UTF-8, replace 兜底极端控制台代码页。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只报告不一致, 不写文件")
    args = parser.parse_args()

    ver = pkg_version()
    changed = sync(ver, check=args.check)
    if changed:
        action = "需要同步" if args.check else "已同步"
        print(f"{action} → v{ver}:")
        for rel in changed:
            print(f"  - {rel}")
        if args.check:
            return 1
    else:
        print(f"版本一致: v{ver} (VERSION / pyproject / _FALLBACK_VERSION)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
