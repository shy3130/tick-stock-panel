"""Inspect installed dependencies without installing or importing their code."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
import sysconfig
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import PackageReference, RuntimeReference


def _editable_project(distribution):
    if _in_frozen_image(distribution):
        # The build environment's direct_url.json never describes deployed PYZ modules.
        return None
    try:
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        if not direct.get("dir_info", {}).get("editable"):
            return None
        url = urlsplit(direct["url"])
        if url.scheme != "file" or url.netloc not in ("", "localhost"):
            raise ValueError("editable source must be local")
        location = unquote(url.path)
        if os.name == "nt" and len(location) > 2 and location[0] == "/" and location[2] == ":":
            location = location[1:]
        root = Path(location).resolve()
        path = root / "pyproject.toml"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            raise ValueError("missing static source project")
        project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        if (
            canonicalize_name(project["name"]) != canonicalize_name(distribution.metadata["Name"])
            or project["version"] != distribution.version
            or not (root / "src").is_dir()
            or (root / "src").is_symlink()
            or "dependencies" in project.get("dynamic", ())
        ):
            raise ValueError("unsupported or stale editable project")
        return root, project
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise TiveloError(
            ErrorCode.RUNTIME_MISMATCH,
            "Editable distribution needs a static src project and matching metadata",
        ) from exc


def _active_requirement(text, extras, environment):
    requirement = Requirement(text)
    if requirement.marker and not any(
        requirement.marker.evaluate({**environment, "extra": extra}) for extra in ("", *extras)
    ):
        return None
    return (
        canonicalize_name(requirement.name),
        tuple(sorted(requirement.extras)),
        str(requirement.specifier),
        requirement.url,
    )


def _check_editable_requirements(distribution, extras, environment):
    editable = _editable_project(distribution)
    if editable is None:
        return
    _, project = editable
    expected = list(project.get("dependencies", ()))
    optional = {
        canonicalize_name(name): values
        for name, values in project.get("optional-dependencies", {}).items()
    }
    for extra in extras:
        expected.extend(optional.get(extra, ()))
    try:
        desired = {_active_requirement(text, extras, environment) for text in expected} - {None}
        actual = {
            _active_requirement(text, extras, environment) for text in distribution.requires or ()
        } - {None}
    except InvalidRequirement as exc:
        raise TiveloError(
            ErrorCode.RUNTIME_MISMATCH, "Editable distribution has invalid dependency metadata"
        ) from exc
    if desired != actual:
        raise TiveloError(
            ErrorCode.RUNTIME_MISMATCH,
            "Editable dependency metadata is stale; rebuild the environment",
        )


def resolve_requirements(requirements: tuple[str, ...]) -> tuple[metadata.Distribution, ...]:
    """Resolve the installed closure, checking markers, extras and version bounds."""
    environment = default_environment()
    resolved = {}
    enabled_extras = {}
    pending = [(text, frozenset()) for text in requirements]
    while pending:
        text, requesting_extras = pending.pop()
        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid runtime requirement") from exc
        if requirement.url is not None:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Runtime requirements cannot install or resolve URLs"
            )
        if requirement.marker and not any(
            requirement.marker.evaluate({**environment, "extra": extra})
            for extra in ("", *requesting_extras)
        ):
            continue
        name = canonicalize_name(requirement.name)
        try:
            distribution = resolved.get(name) or metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise TiveloError(
                ErrorCode.RUNTIME_MISMATCH, "A required distribution is not installed"
            ) from exc
        python_requirement = distribution.metadata.get("Requires-Python")
        try:
            if python_requirement and not SpecifierSet(python_requirement).contains(
                platform.python_version(), prereleases=True
            ):
                raise TiveloError(
                    ErrorCode.RUNTIME_MISMATCH, "Distribution requires a different Python version"
                )
        except InvalidSpecifier as exc:
            raise TiveloError(
                ErrorCode.RUNTIME_MISMATCH, "Distribution has invalid Python compatibility metadata"
            ) from exc
        if not requirement.specifier.contains(distribution.version, prereleases=True):
            raise TiveloError(
                ErrorCode.RUNTIME_MISMATCH, "An installed distribution has an incompatible version"
            )
        extras = {canonicalize_name(extra) for extra in requirement.extras}
        offered = {
            canonicalize_name(extra)
            for extra in distribution.metadata.get_all("Provides-Extra", [])
        }
        if not extras <= offered:
            raise TiveloError(
                ErrorCode.RUNTIME_MISMATCH, "A required distribution extra is unavailable"
            )
        previous = enabled_extras.get(name)
        if previous is not None and extras <= previous:
            continue
        enabled_extras[name] = (previous or set()) | extras
        _check_editable_requirements(distribution, enabled_extras[name], environment)
        resolved[name] = distribution
        pending.extend(
            (child, frozenset(enabled_extras[name])) for child in distribution.requires or ()
        )
    return tuple(resolved[name] for name in sorted(resolved))


_SKIP_DIRECTORIES = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
_GENERATED_METADATA = {
    "RECORD",
    "INSTALLER",
    "REQUESTED",
    "direct_url.json",
    "uv_build.json",
    "uv_cache.json",
}


def _tree_files(root: Path, label: str, *, skip=()):
    if root.is_symlink() or not root.is_dir():
        raise TiveloError(ErrorCode.RUNTIME_MISMATCH, "Runtime source tree is unavailable")
    files = {}
    for current, directories, names in os.walk(root):
        directories[:] = sorted(
            name for name in directories if name not in _SKIP_DIRECTORIES and name not in skip
        )
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise TiveloError(
                    ErrorCode.RUNTIME_MISMATCH, "Runtime tree contains a symbolic directory"
                )
        for name in names:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(current) / name
            files[label + "/" + path.relative_to(root).as_posix()] = path
    return files


def _file_record(label: str, path: Path):
    before = path.stat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise TiveloError(ErrorCode.RUNTIME_MISMATCH, "Runtime contains an unverifiable file")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise TiveloError(ErrorCode.RUNTIME_MISMATCH, "Runtime file changed during verification")
    return dict(path=label, size_bytes=after.st_size, sha256=digest)


def _tree_digest(files):
    values = [_file_record(label, path) for label, path in sorted(files.items())]
    encoded = json.dumps(
        {"format": "tivelo-file-tree-v1", "files": values},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def package_reference(distribution: metadata.Distribution) -> PackageReference:
    """Hash actual installed files, or an explicitly identified editable source tree.

    Wheel digests are never inferred from an installed package. Generated launch
    wrappers, bytecode and installer bookkeeping are excluded; entrypoint and
    dependency metadata, source, resources and native binaries remain included.
    """
    try:
        if not distribution.files:
            raise TiveloError(
                ErrorCode.RUNTIME_MISMATCH, "Distribution has no installed file inventory"
            )
        root = Path(distribution.locate_file("")).resolve()
        editable = _editable_project(distribution)
        files, package_roots, metadata_roots, pth_files = {}, set(), set(), []
        entrypoints = {
            entry.name
            for entry in distribution.entry_points
            if entry.group in ("console_scripts", "gui_scripts")
        }
        wrappers = {
            name + suffix
            for name in entrypoints
            for suffix in ("", ".exe", ".cmd", "-script.py", "-script.pyw")
        }
        for file in distribution.files:
            relative = Path(str(file))
            if relative.is_absolute():
                raise TiveloError(
                    ErrorCode.RUNTIME_MISMATCH, "Distribution has an absolute file record"
                )
            if ".." in relative.parts:
                # Installers regenerate these wrappers with environment-specific shebangs.
                if relative.parent.name.casefold() in ("bin", "scripts"):
                    if relative.name in wrappers:
                        continue
                    script = Path(distribution.locate_file(relative))
                    if script.parent.resolve() == Path(sysconfig.get_path("scripts")).resolve():
                        # Some wheels ship direct scripts instead of entry points.
                        files["scripts/" + relative.name] = script
                        continue
                raise TiveloError(
                    ErrorCode.RUNTIME_MISMATCH, "Distribution owns an unsupported external file"
                )
            if "__pycache__" in relative.parts or relative.suffix in (".pyc", ".pyo"):
                continue
            if relative.parts[0].endswith((".dist-info", ".egg-info")):
                metadata_roots.add(relative.parts[0])
                if relative.name in _GENERATED_METADATA:
                    continue
            elif editable and relative.suffix == ".pth":
                pth_files.append(root / relative)
                continue
            elif len(relative.parts) > 1:
                package_roots.add(relative.parts[0])
            files["installed/" + relative.as_posix()] = root / relative
        # Additional files inside an owned package can change imports despite
        # not appearing in RECORD. Bind them too, excluding only generated caches.
        for name in package_roots:
            files.update(_tree_files(root / name, "installed/" + name))
        for name in metadata_roots:
            for label, path in _tree_files(root / name, "installed/" + name).items():
                if path.parent == root / name and path.name in _GENERATED_METADATA:
                    continue
                files[label] = path
        if editable:
            source, project = editable
            if len(pth_files) != 1 or pth_files[0].is_symlink():
                raise TiveloError(
                    ErrorCode.RUNTIME_MISMATCH, "Editable import layout is not supported"
                )
            paths = [
                line.strip()
                for line in pth_files[0].read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if len(paths) != 1 or Path(paths[0]).resolve() != (source / "src").resolve():
                raise TiveloError(
                    ErrorCode.RUNTIME_MISMATCH,
                    "Editable importer does not bind the declared source tree",
                )
            files.update(_tree_files(source / "src", "source/src"))
            files["source/pyproject.toml"] = source / "pyproject.toml"
            for value in (project.get("readme"), project.get("license")):
                relative = (
                    value.get("file")
                    if isinstance(value, dict)
                    else (value if value == project.get("readme") else None)
                )
                if relative:
                    path = source / relative
                    if not path.resolve().is_relative_to(source):
                        raise TiveloError(
                            ErrorCode.RUNTIME_MISMATCH, "Editable metadata file escapes the project"
                        )
                    files["source/" + Path(relative).as_posix()] = path
        if not files:
            raise TiveloError(ErrorCode.RUNTIME_MISMATCH, "Distribution has no verifiable files")
        return PackageReference(
            name=canonicalize_name(distribution.metadata["Name"]),
            version=distribution.version,
            sha256=_tree_digest(files),
            artifact_kind="source_tree_v1" if editable else "installed_tree_v1",
        )
    except (OSError, ValueError, TypeError) as exc:
        raise TiveloError(
            ErrorCode.RUNTIME_MISMATCH, "Installed distribution failed content verification"
        ) from exc


@dataclass(frozen=True)
class CapturedRuntime:
    packages: tuple[PackageReference, ...]
    reference: RuntimeReference


def _in_frozen_image(distribution):
    return getattr(sys, "frozen", False) and (
        Path(distribution.locate_file("")).resolve() == Path(sys._MEIPASS).resolve()
    )


@lru_cache(maxsize=16384)
def _cached_image_file(path, identity):
    # Reuse bytes only while the same physical file and change times are observed.
    return _file_record("image", path)["sha256"]


def _image_file(path):
    # On Windows ctime tracks creation, and equal modification times can
    # describe different bytes even when size and file identity match.
    if os.name == "nt":
        return _file_record("image", path)["sha256"]
    info = path.stat()
    return _cached_image_file(
        path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    )


def _frozen_image():
    """Bind the PYZ executable, stdlib and actual deployed native libraries/resources.

    macOS bundles split the onedir tree between Frameworks and Resources using
    relative symlinks. Both the links and their physical targets belong to the lock.
    Never walk the executable's user-writable data directory in an onedir install.
    """
    executable = Path(sys.executable).resolve()
    internal = Path(sys._MEIPASS).resolve()
    contents = executable.parent.parent
    root = (
        contents if executable.parent.name == "MacOS" and contents.name == "Contents" else internal
    )
    digest = hashlib.sha256(b"tivelo-frozen-image-v1\0")
    python = _image_file(executable)
    digest.update(python.encode("ascii"))

    def fail(error):
        raise error

    for directory, directories, names in os.walk(root, onerror=fail):
        directories.sort()
        for name in sorted([*directories, *names]):
            path = Path(directory) / name
            label = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                if os.path.isabs(target) or not path.resolve(strict=True).is_relative_to(root):
                    raise TiveloError(
                        ErrorCode.RUNTIME_MISMATCH, "Frozen runtime link leaves its image"
                    )
                item = ("link", label, target)
            elif path.is_dir():
                continue
            else:
                item = ("file", label, _image_file(path))
            digest.update(
                json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
            )
    return python, digest.hexdigest()


def capture_runtime(requirements: tuple[str, ...] = ("tivelo-backtest",)) -> CapturedRuntime:
    distributions = resolve_requirements(requirements)
    if not distributions:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "A runtime lock requires installed distributions"
        )
    frozen = getattr(sys, "frozen", False)
    try:
        if frozen:
            python, stdlib_digest = _frozen_image()
        packages = tuple(
            PackageReference(
                name=canonicalize_name(dist.metadata["Name"]),
                version=dist.version,
                sha256=stdlib_digest,
                artifact_kind="frozen_image_v1",
            )
            if _in_frozen_image(dist)
            else package_reference(dist)
            for dist in distributions
        )
    except (OSError, RuntimeError) as exc:
        raise TiveloError(
            ErrorCode.RUNTIME_MISMATCH, "Frozen runtime files are unavailable"
        ) from exc
    inventory = json.dumps(
        {
            "format": "tivelo-installed-runtime-v1",
            "packages": [p.model_dump(mode="json") for p in packages],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not frozen:
        python, stdlib_digest = _installed_interpreter()
    reference = RuntimeReference(
        python_version=platform.python_version(),
        python_abi=sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag,
        platform_tag=f"{sysconfig.get_platform()}|{platform.system()}-{platform.release()}|{platform.machine()}",
        dependency_lock_sha256=hashlib.sha256(inventory).hexdigest(),
        python_sha256=python,
        stdlib_sha256=stdlib_digest,
    )
    return CapturedRuntime(packages, reference)


def _installed_interpreter():
    try:
        stdlib = Path(sysconfig.get_path("stdlib")).resolve()
        files = _tree_files(stdlib, "stdlib", skip=("site-packages", "dist-packages"))
        if os.name == "nt":
            dlls = Path(sys.base_prefix) / "DLLs"
            if dlls.is_dir():
                files.update(_tree_files(dlls, "interpreter/DLLs"))
            for path in Path(sys.base_prefix).glob("python*.dll"):
                files["interpreter/" + path.name] = path.resolve()
        library, library_dir = (
            sysconfig.get_config_var("LDLIBRARY"),
            sysconfig.get_config_var("LIBDIR"),
        )
        if library and library_dir:
            path = Path(library_dir) / library
            if path.is_file():
                files["interpreter/" + path.name] = path.resolve()
        python = _file_record("python", Path(sys.executable).resolve())["sha256"]
        stdlib_digest = _tree_digest(files)
    except OSError as exc:
        raise TiveloError(
            ErrorCode.RUNTIME_MISMATCH, "Python runtime files are unavailable"
        ) from exc
    return python, stdlib_digest


def verify_runtime(
    expected: CapturedRuntime, requirements: tuple[str, ...] = ("tivelo-backtest",)
) -> None:
    if capture_runtime(requirements) != expected:
        raise TiveloError(
            ErrorCode.RUNTIME_MISMATCH, "Runtime differs from the locked content and environment"
        )
