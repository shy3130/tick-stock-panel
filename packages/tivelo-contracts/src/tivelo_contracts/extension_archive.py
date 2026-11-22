"""Bounded, static ZIP inspection shared by the workbench and engine hosts."""

import io
import stat
import zipfile
from pathlib import PurePosixPath

MAX_UPLOAD = 20 * 1024 * 1024
MAX_EXPANDED = 80 * 1024 * 1024


def read_engine_archive(data: bytes) -> dict[str, bytes]:
    """Read a candidate into memory for inspection. Never extract or import a member."""
    return read_archive(data, {".json", ".so", ".pyd", ".dylib", ".dll"}, "引擎包包含未支持的文件类型")


def read_archive(data: bytes, suffixes: set[str], invalid_type: str) -> dict[str, bytes]:
    if len(data) > MAX_UPLOAD:
        raise ValueError("插件包超过 20 MiB")
    files: dict[str, bytes] = {}
    folded: set[str] = set()
    total = 0
    reserved = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 1000:
                raise ValueError("插件包条目超过 1000")
            for info in archive.infolist():
                name = info.filename
                parts = PurePosixPath(name).parts
                if (not parts or info.orig_filename != name or name.startswith("/") or "\\" in name or ":" in name
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
                    raise ValueError(invalid_type)
                files[name] = archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("无法读取 ZIP 插件包") from exc
    # Also reject a file being used as another member's parent, on every OS.
    names = {name.casefold() for name in files}
    for name in files:
        if any(parent.as_posix().casefold() in names for parent in PurePosixPath(name).parents):
            raise ValueError("插件包的文件与目录路径冲突")
    return files
