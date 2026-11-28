import json
import os
import subprocess
import sys

from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.data import BarQuery


def test_core_cli_describes_missing_http_extra_before_reading_source(tmp_path, capsys, monkeypatch):
    from tivelo_data.cli import main

    monkeypatch.setitem(sys.modules, "yaml", None)
    root = tmp_path / "uncreated"
    assert (
        main(["--root", str(root), "plan-http", "--capture", "unused", "--source", "unused"]) == 1
    )
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == "UNSUPPORTED_CAPABILITY" and "tivelo-data[cn-http]" in error["message"]
    assert not root.exists()


def test_cli_preserves_explicit_empty_event_evidence(make_engine, tmp_path):
    from tivelo_contracts.data import Coverage, CoverageIssue

    from tivelo_data import DataEngine

    _, _, plan = make_engine((), record_type="corporate_action")
    request, source, evidence = (
        tmp_path / name for name in ("request.json", "empty.jsonl", "coverage.json")
    )
    request.write_bytes(canonical_bytes(plan.request))
    source.write_text("", encoding="utf-8")
    coverage = Coverage(
        status="empty",
        requested_units=1,
        completed_units=1,
        row_count=0,
        issues=(
            CoverageIssue(
                instrument_id="security-1",
                reason="no_events",
                detail="Synthetic source confirms no events",
            ),
        ),
    )
    evidence.write_bytes(canonical_bytes(coverage))
    root = tmp_path / "events"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tivelo_data.cli",
            "--root",
            str(root),
            "import-jsonl",
            "--request",
            str(request),
            "--input",
            str(source),
            "--source-id",
            "fixture",
            "--knowledge-basis",
            "historical_versions",
            "--coverage",
            str(evidence),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    snapshot_id = json.loads(result.stdout)["snapshot_id"]
    store = DataEngine(root).store
    snapshot = store.snapshot(snapshot_id)
    assert (
        store.dataset_version(snapshot.contents.datasets[0].version_id).contents.coverage
        == coverage
    )


def test_cli_plan_import_and_query_without_a_running_workbench(make_bar, make_engine, tmp_path):
    _, _, plan = make_engine((make_bar(),))
    request = tmp_path / "request.json"
    request.write_bytes(canonical_bytes(plan.request))
    source = tmp_path / "bars.jsonl"
    source.write_bytes(canonical_bytes(make_bar()) + b"\n")
    root = tmp_path / "cli-store"
    prefix = [sys.executable, "-m", "tivelo_data.cli", "--root", str(root)]
    options = [
        "--request",
        str(request),
        "--input",
        str(source),
        "--source-id",
        "fixture",
        "--knowledge-basis",
        "historical_versions",
        "--bar-convention",
        "synthetic.day",
    ]
    planned = subprocess.run(
        [*prefix, "plan", *options],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    assert json.loads(planned.stdout)["request"]["dataset_id"] == "bar"
    assert not root.exists()
    imported = subprocess.run(
        [*prefix, "import-jsonl", *options],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    snapshot = json.loads(imported.stdout)["snapshot_id"]
    query = BarQuery(
        instrument_ids=("security-1",),
        timeframe="1d",
        start="2025-01-01T00:00:00Z",
        end="2025-02-01T00:00:00Z",
        as_of="2025-01-03T00:00:00Z",
    )
    query_file = tmp_path / "query.json"
    query_file.write_bytes(canonical_bytes(query))
    result = subprocess.run(
        [*prefix, "query", "--snapshot", snapshot, "--request", str(query_file)],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    assert json.loads(result.stdout)["close"] == "11"
    output = tmp_path / "portable"
    exported = subprocess.run(
        [*prefix, "export", "--snapshot", snapshot, "--output", str(output)],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    assert json.loads(exported.stdout)["snapshot"]["snapshot_id"] == snapshot
    assert json.loads((output / "bundle.json").read_text())["snapshot"]["snapshot_id"] == snapshot


def test_cli_errors_do_not_dump_invalid_source_payload(make_bar, make_engine, tmp_path):
    _, _, plan = make_engine((make_bar(),))
    request = tmp_path / "request.json"
    request.write_bytes(canonical_bytes(plan.request))
    source = tmp_path / "bad.jsonl"
    source.write_text('{"source_password":"DO_NOT_DISCLOSE"}\n')
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tivelo_data.cli",
            "--root",
            str(tmp_path / "store"),
            "import-jsonl",
            "--request",
            str(request),
            "--input",
            str(source),
            "--source-id",
            "fixture",
            "--knowledge-basis",
            "unknown",
            "--bar-convention",
            "synthetic.day",
        ],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 1
    assert "DO_NOT_DISCLOSE" not in result.stderr + result.stdout
    assert json.loads(result.stderr)["code"] == "CONTRACT_INVALID"
