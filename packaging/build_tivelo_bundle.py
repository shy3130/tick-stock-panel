"""Build the five wheels and the desktop compatibility lock from this checkout."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from tivelo_backtest.runtime import resolve_requirements
from tivelo_contracts import API_MAJOR

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.desktop_engines.bundle import (  # noqa: E402
    DISTRIBUTIONS,
    ENGINES,
    CompatibilityLock,
    Wheel,
    digest,
    runtime_requirements,
    target,
    wheel_files,
)


def verify_frozen_sources(wheel, archive):
    """The three host-fixed components must be frozen from the same public artifact."""
    namespace = wheel.name.replace("-", "_")
    folder = Path(find_spec(namespace).origin).parent
    files = wheel_files(wheel, archive)
    for member, expected in files.items():
        if member.startswith(namespace + "/"):
            deployed = folder.parent / member
            if (
                member == "tivelo_contracts/wire/contracts-v2.schema.json"
                and not deployed.exists()
            ):
                deployed = (
                    ROOT / "packages/tivelo-contracts/schemas/contracts-v2.schema.json"
                )
            if deployed.read_bytes() != expected:
                raise ValueError(
                    f"Frozen source differs from {wheel.filename}: {member}"
                )
    if any(
        path.relative_to(folder.parent).as_posix() not in files
        for path in folder.rglob("*.py")
    ):
        raise ValueError(
            f"Build environment contains unrecorded modules for {wheel.name}"
        )


def build(folder):
    folder.mkdir(parents=True, exist_ok=True)
    wheelhouse = os.environ.get("TIVELO_DESKTOP_WHEELHOUSE")
    wheels = []
    for name in DISTRIBUTIONS:
        # An isolated per-project output avoids silently picking a stale version.
        project = ROOT / "packages" / name
        version = tomllib.loads(
            (project / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        path = folder / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        if wheelhouse:
            shutil.copyfile(Path(wheelhouse) / path.name, path)
        else:
            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", str(folder), str(project)],
                check=True,
            )
        dist, parsed, _, _ = parse_wheel_filename(path.name)
        wheels.append(
            Wheel(
                name=dist,
                version=str(parsed),
                filename=path.name,
                sha256=digest(path.read_bytes()),
            )
        )
        if name in ENGINES:
            wheel_files(wheels[-1], path.read_bytes())
        else:
            verify_frozen_sources(wheels[-1], path.read_bytes())
    dependencies = {
        canonicalize_name(d.metadata["Name"]): d.version
        for d in resolve_requirements(
            runtime_requirements(
                legacy_cpu=find_spec("_polars_runtime_compat") is not None
            )
        )
    }
    if any(dependencies[w.name] != w.version for w in wheels):
        raise ValueError(
            "Installed desktop build dependencies differ from the packaged releases"
        )
    host = tomllib.loads((ROOT / "backend/pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(line) for line in host["project"]["dependencies"]]
    lock = CompatibilityLock(
        format="tivelo-desktop-combination-v1",
        workbench=json.loads(
            (ROOT / "frontend/package.json").read_text(encoding="utf-8")
        )["version"],
        runtime=target(),
        contracts_api=API_MAJOR,
        extension_api=1,
        research="tivelo-backtest",
        extension_kinds=("dataset_provider", "price_view"),
        dependency_lock_sha256=digest((ROOT / "backend/uv.lock").read_bytes()),
        dependencies=dependencies,
        wheels=tuple(wheels),
        engine_requirements={
            r.name: str(r.specifier) for r in requirements if r.name in ENGINES
        },
    )
    (folder / "compatibility-lock.json").write_text(
        json.dumps(
            lock.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output.resolve())
