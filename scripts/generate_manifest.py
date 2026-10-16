#!/usr/bin/env python3
"""生成升级保护清单 — 全量程序文件 sha256 基线 (app/upgrade_manifest.json)。

供发布流水线调用 (release.yml 桌面打包 / Dockerfile 镜像构建), 运行时由
app/services/upgrade_guard.py 读取:
  scripts/generate_manifest.py --root /app --output /app/app/upgrade_manifest.json \
      --version-file VERSION

排除规则必须与 upgrade_guard.EXCLUDED_* 保持同集, 否则会把运行期产物
当成用户改动。清单自身也不入清单。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_DIRS = {"data", ".venv", "venv", "node_modules", "__pycache__", ".git", "logs"}
EXCLUDED_NAMES = {".env", ".DS_Store", "upgrade_manifest.json"}
EXCLUDED_SUFFIXES = {".pyc", ".log", ".tmp"}


def _is_excluded(rel: str, name: str) -> bool:
    if rel in EXCLUDED_DIRS or rel.split("/", 1)[0] in EXCLUDED_DIRS:
        return True
    if name in EXCLUDED_NAMES:
        return True
    return Path(name).suffix in EXCLUDED_SUFFIXES


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def generate(root: Path, *, version: str) -> dict[str, object]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _is_excluded(rel, path.name):
            continue
        files[rel] = _sha256(path)
    return {
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file_count": len(files),
        "files": files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="程序文件根目录 (清单的对比基准)")
    parser.add_argument("--output", required=True, type=Path, help="清单输出路径 (应位于 <root>/app/ 下)")
    parser.add_argument("--version", default="", help="版本号 (与 --version-file 二选一)")
    parser.add_argument("--version-file", type=Path, help="从文件读版本 (如仓库根 VERSION)")
    args = parser.parse_args(argv)

    version = args.version.strip()
    if not version and args.version_file and args.version_file.is_file():
        version = args.version_file.read_text(encoding="utf-8").strip()
    if not version:
        parser.error("需要 --version 或可读的 --version-file")

    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"--root 不是目录: {root}")

    manifest = generate(root, version=version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"manifest written: {args.output} ({manifest['file_count']} files, version {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
