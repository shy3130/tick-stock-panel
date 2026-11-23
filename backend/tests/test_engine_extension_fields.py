"""Existing extension-table writer to the real research worker and standalone replay."""

import json
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_engine_jobs import drain, preparing_request
from test_engine_jobs import make_manager as make_manager
from test_engine_jobs import seed as seed
from tivelo_backtest.archives import read_result
from tivelo_backtest.definitions import seal_definition
from tivelo_backtest.runner import execute_run
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.extension_fields import ExtensionQuery
from tivelo_data import extension_capture
from tivelo_data.cli import main as data_cli

from app.api.engine_jobs import router
from app.backtest.worker import run_worker_task
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, rows_to_parquet


@pytest.mark.parametrize("controlled", [False, True], ids=("python", "controlled"))
def test_tsp_ranking_capture_changes_real_fill_only_after_publication(
    tmp_path, make_manager, seed, monkeypatch, capsys, controlled
):
    manager = make_manager(worker_runner=run_worker_task)
    repo = manager.store.data_dir
    inputs = manager.store.path("inputs")
    examples = (
        Path(__file__).resolve().parents[2] / "packages/tivelo-data/examples/tsp-extension-field"
    )
    table = ExtConfig("hot", "人气排名", "timeseries", [ExtField("rank", "int")])
    ExtConfigStore(repo).create(table)
    mapping = examples / "mapping.json"
    clock = datetime(2025, 1, 2, 2, tzinfo=UTC)
    monkeypatch.setattr(extension_capture, "_stamp", lambda: clock)

    def observe(first):
        rows_to_parquet(
            [{"symbol": "600000.SH", "rank": first}, {"symbol": "600001.SH", "rank": 4}],
            table,
            repo,
            date(2025, 1, 2),
        )
        assert (
            data_cli(
                [
                    "--root",
                    str(tmp_path / "witness"),
                    "observe-tsp-field",
                    "--repository",
                    str(repo),
                    "--mapping",
                    str(mapping),
                    "--output",
                    str(inputs),
                ]
            )
            == 0
        )
        return json.loads(capsys.readouterr().out)

    before = observe(1)
    clock = clock.replace(hour=6)
    after = observe(9)
    assert before["field_reference"] == after["field_reference"]
    ref = before["field_reference"]
    base = preparing_request(manager, seed).model_dump(mode="json")
    prepared = base["nodes"][0]["prepare"]
    # Both synthetic instruments share the A-share price, fee and session fixture.
    for source in prepared["datasets"]:
        file = inputs / source["path"]
        rows = [json.loads(line) for line in file.read_text().splitlines() if line]
        twins = [{**row, "instrument_id": "B"} for row in rows]
        file.write_text("".join(json.dumps(row) + "\n" for row in [*rows, *twins]))
        source["request"]["instrument_ids"] = ["A", "B"]
        coverage = source["coverage"]
        for name in ("row_count", "requested_units", "completed_units"):
            coverage[name] *= 2
    strategy = inputs / "rank-strategy"
    strategy.mkdir()
    filename = "program.json" if controlled else "strategy.py"
    shutil.copyfile(examples / filename, strategy / filename)
    definition = seal_definition(
        strategy,
        definition_id="example.rank",
        version="1",
        kind="strategy",
        language="controlled_daily_v1" if controlled else "trusted_python",
        entrypoint=filename if controlled else "strategy:build",
        files=(filename,),
    )
    (strategy / "definition.json").write_bytes(canonical_bytes(definition))
    prepared["definition"] = "rank-strategy"
    prepared["dependency_root"] = None
    prepared["run"] = {
        **json.loads((seed / "request.json").read_text()),
        "universe": ["A", "B"],
        "extension_fields": [ref],
        "strategy_parameters_json": "{}"
        if controlled
        else json.dumps({"field_sha256": ref["field_sha256"]}),
    }
    sources = prepared["datasets"]
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    old = None
    with TestClient(app) as api:
        for key, witnessed, chosen in (("first-rank", before, "A"), ("revised-rank", after, "B")):
            base["idempotency_key"] = key
            prepared["datasets"] = sorted(
                [*sources, witnessed["input"]], key=lambda s: s["request"]["dataset_id"]
            )
            preview = api.post("/api/engine-jobs/preview", json=base)
            assert preview.status_code == 200, preview.text
            plan = preview.json()
            assert plan["plan"]["plan_version"] == "1.4"
            submitted = api.post(
                "/api/engine-jobs", json=base, params={"expected_plan_id": plan["plan_id"]}
            )
            assert submitted.status_code == 200, submitted.text
            state = drain(manager, submitted.json()["job_id"], maximum=64)
            assert state["status"] == "succeeded", state
            nodes = state["nodes"]
            assert nodes["daily.execute"]["attempts"][0]["worker"]["pid"] != os.getpid()
            paths = {
                k: manager.store.path(nodes["daily." + k]["artifact"]["path"])
                for k in ("snapshot", "prepare", "execute")
            }
            lock, result, _ = read_result(paths["execute"])
            assert lock.contents.spec.extension_fields[0].model_dump() == ref
            if controlled:
                (operand,) = lock.contents.scale_proof.field_operands
                assert (operand.dataset_id, operand.field_sha256, operand.alias) == (
                    ref["dataset_id"],
                    ref["field_sha256"],
                    "ext_rank",
                )
            assert [
                (entry.instrument_id, entry.at.day, entry.quantity_delta)
                for entry in result.ledger
                if entry.kind == "buy"
            ] == [(chosen, 3, 249)]
            with PortableSnapshotReader(paths["snapshot"]) as reader:
                early = ExtensionQuery(
                    **ref,
                    instrument_ids=("A", "B"),
                    effective_at="2025-01-02T05:00:00Z",
                    as_of="2025-01-02T05:00:00Z",
                )
                assert [v.int_value for v in reader.extension_values(early)] == [1, 4]
                reader.prepare(max_bytes=2**20)
                assert [v.int_value for v in reader.extension_values(early)] == [1, 4]
            if old is None:
                old = paths, canonical_bytes(result), lock.contents.snapshot.snapshot_id
            else:
                assert lock.contents.snapshot.snapshot_id != old[2]
    paths, expected, _ = old
    replay = tmp_path / "old-rank-replay"
    execute_run(run=paths["prepare"], snapshot=paths["snapshot"], destination=replay)
    assert canonical_bytes(read_result(replay)[1]) == expected
