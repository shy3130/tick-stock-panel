"""A v1 TSP ZIP and a signed v2 source read the exact same supplier release."""

import hashlib
import json
import os
import runpy
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from test_source_packages import cold_restart, isolated  # noqa: F401 - isolated workbench fixture

from app.api import settings as settings_api
from app.data_providers.capabilities import resolve_capability_sources
from app.data_providers.custom import loader, packages
from app.services import preferences


@pytest.mark.usefixtures("isolated")
def test_external_source_latest_rows_and_independent_locked_versions(tmp_path, monkeypatch):
    build = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "examples/dual-host-data-source/build.py")
    )
    output = tmp_path / "release"
    pairs = build["build"](output)
    monkeypatch.setenv("TIVELO_RELEASE_TRUST_FILE", str(output / "trust.json"))
    interpreter = os.environ.get("TIVELO_DUAL_SOURCE_PYTHON", sys.executable)
    installation, lake = tmp_path / "state", tmp_path / "lake"

    def cli(module, *args):
        p = subprocess.run(
            [interpreter, "-I", "-m", module, *map(str, args)],
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
            timeout=60,
        )
        return [json.loads(line) for line in p.stdout.splitlines()]

    def admin(*args):
        return cli("tivelo_extensions.cli", "--state", installation, *args)[0]

    def acquire(command, *args):
        return cli(
            "tivelo_data.cli",
            "--root",
            lake,
            command,
            "--extension-state",
            installation,
            "--capture",
            output / "capture.json",
            *args,
        )[0]

    def read(snapshot, at):
        query = tmp_path / "query.json"
        request = json.loads((output / "capture.json").read_bytes())["request"]
        query.write_text(
            json.dumps(
                {k: request[k] for k in ("instrument_ids", "start", "end", "timeframe")}
                | {"as_of": at}
            )
        )
        return cli(
            "tivelo_data.cli", "--root", lake, "query", "--request", query, "--snapshot", snapshot
        )

    def probe(price):
        req = settings_api.CustomSourceTestIn(
            provider="example_archive", dataset="daily", symbols=["600000.SH"]
        )
        response = settings_api.test_data_source(req)
        assert response["rows"] == 2
        assert [r["close"] for r in response["preview"]] == [price, price]
        assert [r["volume"] for r in response["preview"]] == [10, 10]
        assert [r["amount"] for r in response["preview"]] == [price * 1000, price * 1000]
        provider = loader.get_provider("example_archive")
        one = provider.get_daily(["600000.SH"], datetime(2025, 1, 3), datetime(2025, 1, 3))
        assert one.height == 1 and provider.get_daily(["600001.SH"]).is_empty()

    digests = []
    locked = tmp_path / "first-plan.json"
    for generation, pair in enumerate(pairs):
        host = output / pair["tsp"]
        engine = output / pair["tivelo"]
        with ZipFile(host) as v1, ZipFile(engine) as v2:
            binary = v1.read("observations.json")
            assert binary == v2.read("payload/observations.json")
            assert hashlib.sha256(binary).hexdigest() == pair["observations_sha256"]
        body = host.read_bytes()
        inspected, _ = packages.inspect_archive(body)
        packages.install(body, inspected["digest"], inspected["revision"])
        info = admin("inspect", "--archive", engine)
        digest = admin(
            "stage", "--archive", engine, "--digest", info["digest"], "--revision", info["revision"]
        )["digest"]
        digests.append(digest)
        admin("switch", "activate", "--digest", digest, "--generation", generation)
        probe(10)
        if generation == 0:
            preferences.save({"daily_data_provider": "example_archive"})
            daily = next(r for r in resolve_capability_sources() if r["id"] == "daily")
            assert daily["usable"] and daily["source"] == "example_archive"
            first = acquire("plan-provider")
            assert first["extension"]["archive_sha256"] == digest
            locked.write_text(json.dumps(first))
        else:
            assert packages.read_state("example_archive")["pending"] == "2.0.0"
            cold_restart()
            probe(12)

    prior = acquire("import-provider", "--locked-plan", locked)["snapshot_id"]
    current = acquire("import-provider")["snapshot_id"]
    assert prior != current
    for snapshot, at, price in (
        (prior, "2025-01-07T00:00:00Z", "10"),
        (current, "2025-01-04T00:00:00Z", "10"),
        (current, "2025-01-07T00:00:00Z", "12"),
    ):
        facts = read(snapshot, at)
        assert [r["close"] for r in facts] == [price, price]
        assert all(
            r["source_id"] == "example.archive"
            and r["volume"] == "1000"
            and r["quantity_unit"] == "share"
            and r["turnover"] == str(int(price) * 1000)
            for r in facts
        )
    packages.manage("example_archive", "rollback")
    cold_restart()
    probe(10)
    assert (
        admin("switch", "rollback", "--digest", digests[1], "--generation", 2)["active_digest"]
        == digests[0]
    )
    assert acquire("plan-provider") == first
    assert [r["close"] for r in read(current, "2025-01-07T00:00:00Z")] == ["12", "12"]
    (tmp_path / "verified.json").write_text(
        json.dumps({"pairs": pairs, "digests": digests, "old": prior, "new": current})
    )
