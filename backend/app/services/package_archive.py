"""Shared static checks for trusted, externally installed source packages."""
from __future__ import annotations

import importlib.metadata
import io
import platform
import stat
import sys
import zipfile
from pathlib import PurePosixPath

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from app import __version__

MAX_UPLOAD = 20 * 1024 * 1024
MAX_EXPANDED = 80 * 1024 * 1024
SOURCE_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".txt", ".md", ".csv"}


def read_code_archive(data: bytes, *, frontend: bool = False) -> dict[str, bytes]:
    if len(data) > MAX_UPLOAD:
        raise ValueError("插件包超过 20 MiB")
    files: dict[str, bytes] = {}
    folded: set[str] = set()
    total = 0
    suffixes = SOURCE_SUFFIXES | ({".js", ".mjs", ".css", ".svg", ".png", ".jpg", ".webp"} if frontend else set())
    reserved = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 1000:
                raise ValueError("插件包条目超过 1000")
            for info in archive.infolist():
                name = info.filename
                parts = PurePosixPath(name).parts
                if (not parts or name.startswith("/") or "\\" in name or ":" in name
                        or any(p in (".", "..") or p.endswith((".", " ")) or p.split(".")[0].upper() in reserved for p in parts)
                        or PurePosixPath(name).as_posix() != name.rstrip("/")):
                    raise ValueError("插件包包含非法路径")
                if stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("插件包不允许符号链接或特殊文件")
                key = name.casefold().rstrip("/")
                if key in folded:
                    raise ValueError("插件包包含重复或大小写冲突的路径")
                folded.add(key)
                if info.is_dir():
                    continue
                total += info.file_size
                if total > MAX_EXPANDED or info.file_size > MAX_UPLOAD:
                    raise ValueError("插件包解压大小超限")
                if PurePosixPath(name).suffix.lower() not in suffixes:
                    raise ValueError("只支持源码和页面产物；不接收原生库、wheel、脚本或可执行文件")
                files[name] = archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("无法读取 ZIP 插件包") from exc
    # Also reject a file being used as another member's parent, on every OS.
    names = set(files)
    for name in files:
        if any(parent.as_posix() in names for parent in PurePosixPath(name).parents):
            raise ValueError("插件包的文件与目录路径冲突")
    return files


def compatibility(doc: dict) -> list[str]:
    errors = []
    for field, current in (("requires_app", __version__), ("requires_python", platform.python_version())):
        if doc.get(field) and Version(current) not in SpecifierSet(str(doc[field])):
            errors.append(f"{field}: 需要 {doc[field]}，当前 {current}")
    platforms = doc.get("platforms", ["any"])
    current = f"{sys.platform}-{platform.machine().lower()}"
    if not isinstance(platforms, list) or not all(isinstance(x, str) for x in platforms):
        raise ValueError("platforms 必须是字符串列表")
    if "any" not in platforms and current not in platforms:
        errors.append(f"运行平台不匹配，当前 {current}")
    requirements = doc.get("requires", [])
    if not isinstance(requirements, list):
        raise ValueError("requires 必须是依赖列表")
    for item in requirements:
        req = Requirement(item)
        if req.url:
            raise ValueError("requires 不接受 URL 或安装脚本")
        if req.marker and not req.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(req.name)
            if installed not in req.specifier:
                errors.append(f"依赖 {req.name} 版本不匹配：{installed}")
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"缺少依赖 {req.name}（不会自动修改主程序环境）")
    return errors
