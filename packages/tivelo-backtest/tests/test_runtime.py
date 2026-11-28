import json
import os
import shutil
import sys
from email.message import Message
from importlib import metadata
from pathlib import Path

import pytest
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_backtest.runtime import (
    capture_runtime,
    package_reference,
    resolve_requirements,
    verify_runtime,
)


class Distribution:
    def __init__(self, name, version="1", requires=(), extras=()):
        self.version, self.requires = version, requires
        self.metadata = Message()
        self.metadata["Name"] = name
        for extra in extras:
            self.metadata["Provides-Extra"] = extra

    def read_text(self, filename):
        return None


class FileDistribution(Distribution):
    def __init__(self, root, *, direct_url=None):
        super().__init__("one")
        self.root = root
        self.direct_url = direct_url
        self.files = [
            metadata.PackagePath("one/__init__.py"),
            metadata.PackagePath("one-1.dist-info/METADATA"),
            metadata.PackagePath("one-1.dist-info/RECORD"),
        ]
        self.entry_points = ()

    def locate_file(self, path):
        return self.root / path

    def read_text(self, filename):
        return (
            json.dumps(self.direct_url)
            if filename == "direct_url.json" and self.direct_url
            else None
        )


def file_distribution(root):
    (root / "one").mkdir(parents=True)
    (root / "one/__init__.py").write_text("VALUE = 1\n")
    (root / "one-1.dist-info").mkdir()
    (root / "one-1.dist-info/METADATA").write_text("Name: one\nVersion: 1\n")
    (root / "one-1.dist-info/RECORD").write_text("generated\n")
    return FileDistribution(root)


def installed(monkeypatch, *distributions):
    values = {distribution.metadata["Name"]: distribution for distribution in distributions}

    def get(name):
        try:
            return values[name]
        except KeyError as exc:
            raise metadata.PackageNotFoundError(name) from exc

    monkeypatch.setattr(metadata, "distribution", get)


def test_installed_dependency_resolution_handles_transitive_extras_markers_and_cycles(monkeypatch):
    installed(
        monkeypatch,
        Distribution("one", requires=("two[fast]>=1", 'ignored; python_version < "3.0"')),
        Distribution("two", requires=('three; extra == "fast"', "one"), extras=("fast",)),
        Distribution("three"),
    )
    actual = resolve_requirements(("one>=1",))
    assert [distribution.metadata["Name"] for distribution in actual] == ["one", "three", "two"]


@pytest.mark.parametrize("requirement", ["missing", "one>1", "one[missing]"])
def test_missing_or_incompatible_runtime_is_not_auto_installed(monkeypatch, requirement):
    installed(monkeypatch, Distribution("one"))
    with pytest.raises(TiveloError) as raised:
        resolve_requirements((requirement,))
    assert raised.value.code == ErrorCode.RUNTIME_MISMATCH


@pytest.mark.parametrize(
    "requirement", ["bad package !!!", "one @ https://example.invalid/one.whl"]
)
def test_requirements_cannot_trigger_remote_resolution(monkeypatch, requirement):
    installed(monkeypatch, Distribution("one"))
    with pytest.raises(TiveloError) as raised:
        resolve_requirements((requirement,))
    assert raised.value.code == ErrorCode.CONTRACT_INVALID


def test_installed_tree_is_relocatable_and_detects_code_and_unrecorded_modules(tmp_path):
    first = file_distribution(tmp_path / "original")
    baseline = package_reference(first)
    assert baseline.artifact_kind == "installed_tree_v1"
    shutil.copytree(first.root, tmp_path / "relocated")
    second = FileDistribution(tmp_path / "relocated")
    assert package_reference(second) == baseline
    (second.root / "one/__pycache__").mkdir()
    (second.root / "one/__pycache__/cache.pyc").write_bytes(b"generated")
    (second.root / "one-1.dist-info/RECORD").write_text("changed generated manifest")
    (second.root / "one-1.dist-info/uv_cache.json").write_text('{"cache":"local-only"}')
    assert package_reference(second) == baseline
    extra_metadata = second.root / "one-1.dist-info/entry_points.txt"
    extra_metadata.write_text("[example]\none=one:factory\n")
    assert package_reference(second).sha256 != baseline.sha256
    extra_metadata.unlink()
    (second.root / "one/added.py").write_text("VALUE = 2\n")
    assert package_reference(second).sha256 != baseline.sha256
    (second.root / "one/added.py").unlink()
    (second.root / "one/__init__.py").write_text("VALUE = 3\n")
    assert package_reference(second).sha256 != baseline.sha256


def test_editable_source_is_hashed_as_source_and_stale_metadata_is_rejected(tmp_path, monkeypatch):
    distribution = file_distribution(tmp_path / "environment")
    source = tmp_path / "source"
    (source / "src/one").mkdir(parents=True)
    (source / "src/one/__init__.py").write_text("VALUE = 1\n")
    project = source / "pyproject.toml"
    project.write_text('[project]\nname="one"\nversion="1"\ndependencies=[]\n')
    distribution.direct_url = {"url": source.as_uri(), "dir_info": {"editable": True}}
    pth = distribution.root / "_editable_one.pth"
    pth.write_text(str(source / "src") + "\n")
    distribution.files = [
        metadata.PackagePath("_editable_one.pth"),
        metadata.PackagePath("one-1.dist-info/METADATA"),
    ]
    installed(monkeypatch, distribution)
    original = package_reference(distribution)
    assert original.artifact_kind == "source_tree_v1"
    (source / "src/one/__init__.py").write_text("VALUE = 2\n")
    assert package_reference(distribution).sha256 != original.sha256
    project.write_text('[project]\nname="one"\nversion="1"\ndependencies=["missing>=1"]\n')
    with pytest.raises(TiveloError) as raised:
        resolve_requirements(("one",))
    assert raised.value.code == ErrorCode.RUNTIME_MISMATCH


def test_direct_installed_script_is_bound_only_inside_environment_scripts(tmp_path, monkeypatch):
    import tivelo_backtest.runtime as runtime

    distribution = file_distribution(tmp_path / "site")
    scripts = tmp_path / "bin"
    scripts.mkdir()
    script = scripts / "helper"
    script.write_text("initial script", encoding="utf-8")
    distribution.files.append(metadata.PackagePath("../bin/helper"))
    monkeypatch.setattr(runtime.sysconfig, "get_path", lambda name: str(scripts))
    original = package_reference(distribution)
    script.write_text("changed script", encoding="utf-8")
    assert package_reference(distribution).sha256 != original.sha256
    monkeypatch.setattr(runtime.sysconfig, "get_path", lambda name: str(tmp_path / "other"))
    with pytest.raises(TiveloError) as raised:
        package_reference(distribution)
    assert raised.value.code == ErrorCode.RUNTIME_MISMATCH


def test_runtime_binds_packages_interpreter_and_stdlib(tmp_path, monkeypatch):
    import tivelo_backtest.runtime as runtime

    distribution = file_distribution(tmp_path / "environment")
    installed(monkeypatch, distribution)
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    (stdlib / "module.py").write_text("VALUE = 1\n")
    executable = tmp_path / "python"
    executable.write_bytes(b"synthetic-interpreter")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(runtime.sysconfig, "get_path", lambda name: str(stdlib))
    original = capture_runtime(("one",))
    verify_runtime(original, ("one",))
    assert original.reference.python_sha256 and original.reference.stdlib_sha256
    (stdlib / "module.py").write_text("VALUE = 2\n")
    with pytest.raises(TiveloError) as raised:
        verify_runtime(original, ("one",))
    assert raised.value.code == ErrorCode.RUNTIME_MISMATCH


def test_unverifiable_runtime_file_is_rejected(tmp_path):
    distribution = file_distribution(tmp_path / "environment")
    (distribution.root / "one/__init__.py").unlink()
    with pytest.raises(TiveloError) as raised:
        package_reference(distribution)
    assert raised.value.code == ErrorCode.RUNTIME_MISMATCH


def test_distribution_python_compatibility_is_checked_before_loading(monkeypatch):
    distribution = Distribution("one")
    distribution.metadata["Requires-Python"] = ">=99"
    installed(monkeypatch, distribution)
    with pytest.raises(TiveloError) as raised:
        resolve_requirements(("one",))
    assert raised.value.code == ErrorCode.RUNTIME_MISMATCH


def test_frozen_lock_follows_deployed_image_and_ignores_adjacent_user_data(tmp_path, monkeypatch):
    from tivelo_backtest.runtime import _frozen_image

    root = tmp_path / "TSP"
    internal = root / "_internal"
    internal.mkdir(parents=True)
    executable = root / "TSP"
    executable.write_bytes(b"interpreter-and-pyz")
    native = internal / "native.runtime"
    native.write_bytes(b"original")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    first = _frozen_image()
    (root / "user-data").write_text("user")
    assert _frozen_image() == first
    native.write_bytes(b"modified")
    assert _frozen_image()[0] == first[0]
    assert _frozen_image()[1] != first[1]
    modified = native.stat()
    native.write_bytes(b"original")
    os.utime(native, ns=(modified.st_atime_ns, modified.st_mtime_ns))
    assert _frozen_image() == first


def test_frozen_macos_bundle_links_bind_targets_and_reject_external_content(tmp_path, monkeypatch):
    from tivelo_backtest.runtime import _frozen_image

    pkg = tmp_path / "TSP.app/Contents"
    binary = pkg / "MacOS/TSP"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"pyz")
    internal, resources = pkg / "Frameworks", pkg / "Resources"
    internal.mkdir()
    resources.mkdir()
    (resources / "preset.json").write_text("v1")
    link = internal / "preset.json"
    try:
        link.symlink_to(Path("..") / "Resources" / "preset.json")
    except OSError:
        pytest.skip("symbolic links unavailable")
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(binary))
    before = _frozen_image()
    (resources / "preset.json").write_text("v2")
    assert before != _frozen_image()
    link.unlink()
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(TiveloError, match="leaves its image"):
        _frozen_image()
