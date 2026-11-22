"""Build/install boundary check, reusable locally and on every CI platform."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")


def audit_public_contents(package, wheel_path, sdist_path):
    """Check the actual archives, their public source bytes and the pure wheel inventory."""
    metadata = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
    src = package / "src"
    expected = {
        file.relative_to(src).as_posix(): file
        for file in src.rglob("*")
        if file.is_file()
        and "__pycache__" not in file.parts
        and file.suffix != ".pyc"
        and not file.name.startswith(".")
    }
    wheel_config = metadata["tool"]["hatch"]["build"]["targets"]["wheel"]
    for source, destination in wheel_config.get("force-include", {}).items():
        expected[destination] = package / source

    def public_member(name, content):
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or any(
                "tivelo_pro_dynamic" in part.lower()
                or "tivelo-pro-dynamic" in part.lower()
                for part in path.parts
            )
            or path.name.lower() == "tivelo_issuers.h"
            or path.suffix.lower() in {".pyx", ".pxi", ".so", ".pyd", ".dll", ".dylib"}
            or PRIVATE_KEY_HEADER.search(content)
        ):
            raise ValueError(f"Invalid public release member: {name}")

    def compare(source, content, name):
        if (
            source is None
            or not source.resolve().is_relative_to(package)
            or not source.is_file()
            or source.read_bytes() != content
        ):
            raise ValueError(f"Release file differs from the public project: {name}")

    metadata_root = wheel_path.name.split("-py", 1)[0] + ".dist-info"
    seen = set()
    generated_metadata = {}
    with zipfile.ZipFile(wheel_path) as wheel:
        for item in wheel.infolist():
            if item.is_dir():
                continue
            name = item.filename
            content = wheel.read(item)
            public_member(name, content)
            if name in seen:
                raise ValueError(f"Duplicate wheel file: {name}")
            seen.add(name)
            if not name.startswith(metadata_root + "/"):
                compare(expected.get(name), content, name)
                continue
            metadata_name = name[len(metadata_root) + 1 :]
            if metadata_name.startswith("licenses/"):
                compare(
                    package / metadata_name.removeprefix("licenses/"), content, name
                )
            elif metadata_name in {"METADATA", "WHEEL", "RECORD", "entry_points.txt"}:
                generated_metadata[metadata_name] = content
            else:
                raise ValueError(f"Unexpected public wheel metadata: {name}")
        if expected.keys() != {
            name for name in seen if not name.startswith(metadata_root + "/")
        }:
            raise ValueError(
                "Public wheel files differ from the declared source inventory"
            )

    prefix = sdist_path.name.removesuffix(".tar.gz")
    source_files = set()
    with tarfile.open(sdist_path, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(prefix + "/"):
                raise ValueError(f"Unexpected source archive member: {member.name}")
            name = member.name[len(prefix) + 1 :]
            if name in source_files:
                raise ValueError(f"Duplicate source archive member: {name}")
            source_files.add(name)
            content = archive.extractfile(member).read()
            public_member(name, content)
            if name == "PKG-INFO":
                if content != generated_metadata.get("METADATA"):
                    raise ValueError("Wheel and source package metadata differ")
                continue
            if name == ".gitignore" and not (package / name).exists():
                # Hatch includes the checkout ignore rules when building within this repository.
                if content != (package.parents[1] / name).read_bytes():
                    raise ValueError("Unexpected source distribution ignore rules")
            else:
                compare(package / name, content, name)
    if metadata["project"]["name"] == "tivelo-data":
        example = "examples/cn-stock-factors/"
        required = {example + filename for filename in ("serve.py", "source.yaml", "capture.json")}
        if not required.issubset(source_files):
            raise ValueError("The source distribution is missing part of the documented stock factor example")
    return {"wheel_files": len(seen), "sdist_files": len(source_files)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--python", default="3.11")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--with-wheel",
        type=Path,
        action="append",
        default=[],
        help="Dependency wheel or directory containing exactly one wheel; repeat for each dependency",
    )
    parser.add_argument("--minimum-pydantic", action="store_true")
    profiles = parser.add_mutually_exclusive_group()
    profiles.add_argument("--extensions", action="store_const", dest="profile", const="extensions")
    profiles.add_argument("--cn-http", action="store_const", dest="profile", const="cn-http")
    for vendor in ("tickflow", "fuyao", "tdxquant"):
        profiles.add_argument(f"--cn-{vendor}", action="store_const", dest="profile", const=f"cn-{vendor}")
    profiles.add_argument("--cn-repository", action="store_const", dest="profile", const="cn-repository")
    args = parser.parse_args()
    package, distribution = args.package.resolve(), args.dist.resolve()
    project = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    name, version = project["name"].replace("-", "_"), project["version"]
    wheels = list(distribution.glob(f"{name}-{version}-*.whl"))
    source = distribution / f"{name}-{version}.tar.gz"
    if len(wheels) != 1 or not source.is_file():
        parser.error(
            "expected exactly one wheel and a source distribution for the package version"
        )
    wheel = wheels[0]
    archive_inventory = audit_public_contents(package, wheel, source)
    local_dependencies = []
    for location in args.with_wheel:
        matches = list(location.glob("*.whl")) if location.is_dir() else [location]
        if len(matches) != 1:
            parser.error("each --with-wheel directory must contain exactly one wheel")
        local_dependencies.append(matches[0].resolve())
    if any(not path.is_file() or path.suffix != ".whl" for path in local_dependencies):
        parser.error("--with-wheel must name an existing wheel")
    supported = {
        "tivelo_contracts": {
            "app",
            "tivelo_data",
            "tivelo_backtest",
            "polars",
            "duckdb",
            "pyarrow",
        },
        "tivelo_extensions": {"app", "fastapi", "tivelo_data", "tivelo_backtest", "tivelo_market_cn", "pyarrow", "duckdb", "polars"},
        "tivelo_data": {"app", "tivelo_backtest", "tivelo_pro_dynamic", "fastapi"},
        "tivelo_market_cn": {
            "app",
            "tivelo_data",
            "tivelo_backtest",
            "fastapi",
            "duckdb",
            "pyarrow",
        },
        "tivelo_backtest": {
            "app",
            "tivelo_data",
            "tivelo_market_cn",
            "tivelo_pro_dynamic",
            "fastapi",
            "duckdb",
            "pyarrow",
        },
    }
    if name not in supported:
        parser.error(f"no independent-installation probe registered for {name}")
    if args.profile == "extensions" and name not in {"tivelo_data", "tivelo_backtest"}:
        parser.error("Signed extension profiles apply to an engine")
    if args.profile not in {None, "extensions"} and name != "tivelo_data":
        parser.error("CN acquisition profiles apply to the data package")
    if name != "tivelo_extensions" and args.profile != "extensions":
        supported[name].add("tivelo_extensions")
    if name == "tivelo_data":
        if args.profile in {None, "extensions"}:
            supported[name].add("tivelo_market_cn")
        if args.profile not in {"cn-http", "cn-tickflow", "cn-fuyao"}:
            supported[name].add("httpx")
        if args.profile != "cn-http":
            supported[name].add("yaml")
        if args.profile != "cn-tdxquant":
            supported[name].add("tdxaidata")
        if args.profile != "cn-tickflow":
            supported[name].add("tickflow")
    dependency_options = []
    if args.minimum_pydantic:
        if name != "tivelo_contracts":
            parser.error("--minimum-pydantic applies to the contracts package")
        requirement = next(
            value for value in project["dependencies"] if value.startswith("pydantic>=")
        )
        lower_bound = re.fullmatch(r"pydantic>=([^,]+),<3", requirement)
        if lower_bound is None:
            parser.error("cannot determine Pydantic lower bound from package metadata")
        dependency_options = ["--with", f"pydantic=={lower_bound.group(1)}"]
    prefix = [
        "uv",
        "run",
        "--isolated",
        "--no-project",
        "--python",
        args.python,
        "--with",
        f"{wheel}[test,{args.profile}]" if args.profile else f"{wheel}[test]",
        *(part for path in local_dependencies for part in ("--with", str(path))),
        *dependency_options,
        "python",
        "-I",
    ]
    checks = []

    def run(label: str, command: list[str], working_directory: str) -> str:
        result = subprocess.run(
            command, cwd=working_directory, text=True, capture_output=True
        )
        print(f"{label}: exit {result.returncode}")
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode:
            print(result.stderr.rstrip())
            raise SystemExit(result.returncode)
        checks.append(label)
        return result.stdout

    with tempfile.TemporaryDirectory(prefix="tivelo-wheel-check-") as directory:
        runtime = json.loads(
            run(
                "isolated_import",
                [
                    *prefix,
                    "-c",
                    (
                        "import importlib.util,json,platform,importlib.metadata as m; "
                        f"import {name}; "
                        f"assert all(importlib.util.find_spec(n) is None for n in {sorted(supported[name])!r}); "
                        f"assert 'site-packages' in {name}.__file__, 'must test the installed wheel'; "
                        "print(json.dumps({'python':platform.python_version(),'os':platform.system(),"
                        "'architecture':platform.machine(),'dependencies':{d.metadata['Name']:d.version "
                        "for d in m.distributions()}}))"
                    ),
                ],
                directory,
            )
        )
        if name == "tivelo_data" and args.profile is None:
            # pytest installs packaging itself. Check the user-facing error in the actual base installation.
            base_prefix = [str(wheel) if part == f"{wheel}[test]" else part for part in prefix]
            capture_file = Path(directory) / "signed-source.json"
            capture_file.write_text(json.dumps({
                "kind": "engine_data_provider", "provider_id": "example.source",
                "request": {"dataset_id": "sample", "record_type": "bar", "market_id": "cn.equity",
                            "asset_class": "equity", "instrument_ids": ["example.stock"],
                            "start": "2025-01-02T00:00:00Z", "end": "2025-01-03T00:00:00Z",
                            "timeframe": "1d", "price_basis": "raw"}
            }), encoding="utf-8")
            run(
                "base_cli_without_test_or_extension_dependencies",
                [*base_prefix, "-c",
                 "import contextlib,io,json,importlib.util; from tivelo_data.cli import main; "
                 "assert importlib.util.find_spec('tivelo_extensions') is None; "
                 "err=io.StringIO()\nwith contextlib.redirect_stderr(err):\n "
                 "code=main(['--root','data','plan-provider','--extension-state','state','--capture','signed-source.json'])\n"
                 "assert code==1 and json.loads(err.getvalue())['code']=='EXTENSION_INCOMPATIBLE'"],
                directory,
            )
        if args.profile == "cn-repository":
            run(
                "installed_cn_repository_profile",
                [*prefix, "-c", "import tivelo_market_cn, tivelo_data.repository_daily"],
                directory,
            )
        if args.profile == "cn-http":
            run(
                "installed_cn_http_profile",
                [*prefix, "-c", "import httpx, yaml, tivelo_market_cn, tivelo_data.http.cn_daily, tivelo_data.http.cn_factors"],
                directory,
            )
        if args.profile == "cn-tickflow":
            run(
                "installed_cn_tickflow_profile",
                [*prefix, "-c", "import tickflow, tivelo_market_cn, tivelo_data.tickflow_daily, tivelo_data.tickflow_factors"],
                directory,
            )
        if args.profile == "cn-fuyao":
            run("installed_fuyao_history_profile",
                [*prefix, "-c", "import httpx, tivelo_market_cn, tivelo_data.fuyao_daily, tivelo_data.fuyao_factors"], directory)
        if args.profile == "cn-tdxquant":
            run("installed_tdx_history_profile",
                [*prefix, "-c", "import tdxaidata, tivelo_market_cn, tivelo_data.tdx_daily, tivelo_data.tdx_factors"], directory)
        tests = package / "tests"
        if name == "tivelo_data" and args.profile == "extensions":
            extracted = Path(directory) / "distribution"
            with tarfile.open(source, "r:gz") as archive:
                archive.extractall(extracted, filter="data")
            tests = extracted / source.name.removesuffix(".tar.gz") / "tests"
            if not (tests / "test_native_source.py").is_file():
                raise ValueError("Source distribution lacks the native provider acceptance suite")
            run(
                "installed_native_source_runtime",
                [*prefix, "-c", "import tivelo_extensions.store, tivelo_data.native_session"],
                directory,
            )
        test_output = run(
            "installed_wheel_tests",
            [*prefix, "-m", "pytest", str(tests), "-q"],
            directory,
        )
        if name == "tivelo_contracts":
            run(
                "published_schema",
                [
                    *prefix,
                    "-m",
                    "tivelo_contracts.schemas",
                    str(package / "schemas/contracts-v2.schema.json"),
                    "--check",
                ],
                directory,
            )
            run(
                "installed_wire_schema",
                [
                    *prefix,
                    "-c",
                    "from importlib.resources import files; from pathlib import Path; "
                    "import sys; schema = files('tivelo_contracts').joinpath('wire','contracts-v2.schema.json'); "
                    "assert schema.read_bytes() == Path(sys.argv[1]).read_bytes()",
                    str(package / "schemas/contracts-v2.schema.json"),
                ],
                directory,
            )
        if name in {"tivelo_data", "tivelo_backtest"}:
            run(
                "cli_entry_point",
                [*prefix[:-2], name.replace("_", "-"), "--help"],
                directory,
            )
    report = dict(
        format="tivelo-distribution-verification-v1",
        package=project["name"],
        version=version,
        dependency_floor_checked=args.minimum_pydantic,
        public_artifact_inventory=archive_inventory,
        artifacts=[
            dict(
                filename=path.name,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
            )
            for path in (wheel, source)
        ],
        local_dependency_wheels=[
            dict(
                filename=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
            )
            for path in local_dependencies
        ],
        runtime=runtime,
        passed_checks=checks,
        test_summary=test_output.strip().splitlines()[-1],
        limitations=[
            "Only this host was executed; configured CI platforms are not claimed as passed"
        ],
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
