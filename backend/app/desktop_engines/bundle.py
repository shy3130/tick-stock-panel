"""Static release lock and immutable pure-Python engine versions for the desktop."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
import sysconfig
import tempfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Literal

from packaging.utils import parse_wheel_filename
from packaging.version import Version
from pydantic import Field, model_validator
from tivelo_contracts import API_MAJOR
from tivelo_contracts.base import Contract, Digest, Key, parse_json
from tivelo_contracts.engine_releases import RUNTIME_GENERATION
from tivelo_contracts.extension_archive import MAX_UPLOAD, read_archive

ENGINES = ("tivelo-data", "tivelo-backtest")
DISTRIBUTIONS = (*ENGINES, "tivelo-contracts", "tivelo-market-cn", "tivelo-extensions")
REQUIREMENTS = (
    "tivelo-data[cn-http,cn-fuyao,cn-tdxquant,cn-tickflow,extensions]",
    "tivelo-backtest[extensions]",
    "tivelo-market-cn",
)


def runtime_requirements(*, legacy_cpu):
    packages = (*REQUIREMENTS, "polars[rtcompat]" if legacy_cpu else "polars")
    return (*packages, "tivelo-extensions[linux-desktop]") if sys.platform == "linux" else packages


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_limited(path: Path, limit: int = MAX_UPLOAD) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("发行文件超过大小限制")
    return value


def target():
    return {
        "generation": RUNTIME_GENERATION,
        "python": platform.python_version(),
        "abi": sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag,
        "platform": sys.platform,
        "machine": platform.machine().lower(),
    }


class Wheel(Contract):
    name: Literal[
        "tivelo-data",
        "tivelo-backtest",
        "tivelo-contracts",
        "tivelo-market-cn",
        "tivelo-extensions",
    ]
    version: Key
    filename: str = Field(max_length=180)
    sha256: Digest

    @model_validator(mode="after")
    def pure_wheel(self):
        expected = f"{self.name.replace('-', '_')}-{self.version}-py3-none-any.whl"
        name, version, _, _ = parse_wheel_filename(self.filename)
        if self.filename != expected or name != self.name or str(version) != self.version:
            raise ValueError("发行文件名、版本或平台标签不匹配")
        return self


class CompatibilityLock(Contract):
    format: Literal["tivelo-desktop-combination-v1"]
    workbench: Key
    runtime: dict[str, str]
    contracts_api: int = Field(strict=True)
    extension_api: Literal[1]
    research: Literal["tivelo-backtest"]
    extension_kinds: tuple[Literal["price_view", "dataset_provider"], ...]
    dependency_lock_sha256: Digest
    dependencies: dict[str, str]
    engine_requirements: dict[str, str]
    wheels: tuple[Wheel, ...]

    @model_validator(mode="after")
    def supported(self):
        if sorted(w.name for w in self.wheels) != sorted(DISTRIBUTIONS):
            raise ValueError("桌面组合缺少发行组件或重复声明组件")
        if self.contracts_api != API_MAJOR or self.runtime != target():
            raise ValueError("桌面组合与当前运行环境不匹配")
        if set(self.engine_requirements) != set(ENGINES):
            raise ValueError("发行清单缺少宿主的引擎兼容范围")
        if set(self.extension_kinds) != {"price_view", "dataset_provider"}:
            raise ValueError("扩展契约不匹配")
        return self


def load_bundle(folder):
    raw = read_limited(folder / "compatibility-lock.json", 128 * 1024)
    lock = CompatibilityLock.model_validate(parse_json(raw.decode("utf-8")))
    return digest(raw), lock


def wheel_files(wheel: Wheel, data: bytes):
    if digest(data) != wheel.sha256:
        raise ValueError("引擎 wheel 摘要不匹配")
    files = read_archive(
        data, {"", ".py", ".pyi", ".typed", ".txt", ".json", ".md"}, "引擎 wheel 文件类型不受支持"
    )
    namespace = wheel.name.replace("-", "_")
    dist_info = f"{namespace}-{wheel.version}.dist-info"
    directories = {}
    for key in files:
        path = PurePosixPath(key)
        if len(path.parts) < 2 or path.parts[0] not in (namespace, dist_info):
            raise ValueError("引擎 wheel 超出组件目录")
        for directory in path.parents:
            spelling = directory.as_posix()
            if directories.setdefault(spelling.casefold(), spelling) != spelling:
                raise ValueError("引擎 wheel 目录大小写冲突")
    try:
        meta = BytesParser().parsebytes(files[f"{dist_info}/METADATA"])
        tags = BytesParser().parsebytes(files[f"{dist_info}/WHEEL"])
        if (
            meta["Name"] != wheel.name
            or Version(meta["Version"]) != Version(wheel.version)
            or tags["Root-Is-Purelib"] != "true"
            or tags.get_all("Tag") != ["py3-none-any"]
            or f"{dist_info}/RECORD" not in files
            or f"{namespace}/__init__.py" not in files
        ):
            raise ValueError("引擎 wheel 元数据与发行清单不匹配")
    except (KeyError, TypeError) as exc:
        raise ValueError("引擎 wheel 缺少必要元数据") from exc
    return files


def version_tree(store: Path, wheel: Wheel, data: bytes) -> Path:
    """Publish a complete verified tree once. Concurrent readers see old or complete."""
    files = wheel_files(wheel, data)
    versions = store / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    if versions.is_symlink():
        raise ValueError("引擎版本目录不能是符号链接")
    tree = versions / wheel.sha256
    if not tree.exists():
        with tempfile.TemporaryDirectory(prefix=".unpack-", dir=versions) as temporary:
            source = Path(temporary) / "tree"
            for name, content in files.items():
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            try:
                source.rename(tree)
            except OSError:
                if not tree.is_dir():
                    raise
    if tree.is_symlink() or not tree.is_dir():
        raise ValueError("引擎版本路径无效")
    seen = set()
    expected_dirs = {str(parent) for key in files for parent in PurePosixPath(key).parents}
    for directory, subdirs, names in os.walk(tree):
        parent = Path(directory)
        if any(
            (parent / name).is_symlink()
            or (parent / name).relative_to(tree).as_posix() not in expected_dirs
            for name in subdirs
        ):
            raise ValueError("引擎版本目录含未声明路径")
        for name in names:
            path = parent / name
            key = path.relative_to(tree).as_posix()
            if (
                key not in files
                or path.is_symlink()
                or not path.is_file()
                or read_limited(path) != files[key]
            ):
                raise ValueError("引擎版本目录内容发生变化")
            seen.add(key)
    if seen != files.keys():
        raise ValueError("引擎版本目录缺少文件")
    return tree
