"""Shared static checks for trusted, externally installed source packages."""
from __future__ import annotations

import importlib.metadata
import platform
import sys

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from tivelo_contracts.extension_archive import MAX_EXPANDED as MAX_EXPANDED
from tivelo_contracts.extension_archive import MAX_UPLOAD as MAX_UPLOAD
from tivelo_contracts.extension_archive import read_archive
from tivelo_contracts.extension_archive import read_engine_archive as read_engine_archive

from app import __version__

SOURCE_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".txt", ".md", ".csv"}


def read_code_archive(data: bytes, *, frontend: bool = False) -> dict[str, bytes]:
    suffixes = SOURCE_SUFFIXES | ({".js", ".mjs", ".css", ".svg", ".png", ".jpg", ".webp"} if frontend else set())
    return read_archive(data, suffixes, "只支持源码和页面产物；不接收原生库、wheel、脚本或可执行文件")


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
