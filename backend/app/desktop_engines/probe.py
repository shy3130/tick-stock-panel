"""Offline functional check used by the release build and each candidate activation."""

import json
import os
import shutil
import tempfile
from importlib import metadata
from pathlib import Path

from .bundle import ENGINES, runtime_requirements


def _workbench_replay(root, portable, archive, expected_lock):
    from app.services.engine_jobs.manager import EngineJobManager
    from app.services.engine_jobs.models import JobRequest, ResearchNode, ResearchReport
    from app.services.engine_jobs.storage import ACTIVE

    manager = EngineJobManager(root / "workbench", autostart=False)
    try:
        shutil.copytree(portable, manager.store.path("inputs/snapshot"))
        shutil.copytree(archive, manager.store.path("inputs/run"))
        request = JobRequest(
            idempotency_key="desktop-conformance",
            nodes=(ResearchNode(node_id="daily", run="run", snapshot="snapshot"),),
        )
        state = manager.submit(request)
        while state["status"] in ACTIVE and manager.run_once():
            state = manager.store.get(state["job_id"])
        if state["status"] in ACTIVE:
            raise ValueError(f"工作台验收任务仍在等待执行资源，可稍后重试（{state['status']}）")
        if state["status"] != "succeeded":
            errors = [
                attempt["error"]["code"]
                for n in state["nodes"].values()
                for attempt in n["attempts"]
                if attempt.get("error")
            ]
            if state.get("error"):
                errors.append(state["error"]["code"])
            raise ValueError(f"工作台子进程回测失败（{'、'.join(errors) or state['status']}）")
        nodes = state["nodes"]
        if any(n["attempts"][0]["worker"]["pid"] == os.getpid() for n in nodes.values()):
            raise ValueError("桌面回测没有经过独立工作进程")
        report = ResearchReport.model_validate_json(
            manager.store.path(nodes["daily.report"]["artifact"]["path"]).read_bytes()
        )
        if (
            report.summary["semantics"]["lock_id"] != expected_lock
            or report.summary["metrics"]["final_equity"] != "5237.58"
        ):
            raise ValueError("工作台报告与独立回测归档不一致")
        return {"stages": sorted(nodes), "lock_id": expected_lock, "final_equity": "5237.58"}
    finally:
        manager.shutdown()


def synthetic_inputs(root, fixture):
    """Publish the public numeric fixture and return its portable snapshot and request."""
    from tivelo_backtest.definitions import seal_definition
    from tivelo_contracts.base import canonical_bytes
    from tivelo_contracts.data import Coverage
    from tivelo_data import DataEngine
    from tivelo_data.jsonl import JSONLinesProvider
    from tivelo_data.providers import FixedSourceResolver, SyncRequest

    fixture = json.loads(fixture.read_text(encoding="utf-8"))
    lake = root / "data"
    versions = []
    for data in fixture["datasets"]:
        request = SyncRequest.model_validate(data["request"])
        input_path = root / (request.dataset_id + ".jsonl")
        input_path.write_text(
            "".join(json.dumps(row) + "\n" for row in data["records"]), encoding="utf-8"
        )
        provider = JSONLinesProvider(
            input_path,
            request,
            source_id=data["source_id"],
            knowledge_basis=data["knowledge_basis"],
            bar_convention_id=data["bar_convention_id"],
            coverage=Coverage.model_validate(data["coverage"]),
        )
        engine = DataEngine(lake, FixedSourceResolver(provider, provider.configuration_digest))
        candidate = engine.sync(engine.plan_sync(request))
        checked = engine.validate(candidate)
        published = engine.publish(candidate)
        if checked != published:
            raise ValueError("候选数据与发布版本不一致")
        versions.append(published.version_id)
    engine = DataEngine(lake)
    snapshot = engine.create_snapshot(tuple(versions))
    portable = root / "snapshot"
    engine.export_snapshot(snapshot.snapshot_id, portable)
    definition = root / "definition"
    definition.mkdir()
    (definition / "program.json").write_text(json.dumps(fixture["program"]), encoding="utf-8")
    manifest = seal_definition(
        definition,
        definition_id="desktop.fixture",
        version="1",
        kind="strategy",
        entrypoint="program.json",
        files=("program.json",),
        language="controlled_daily_v1",
    )
    (definition / "definition.json").write_bytes(canonical_bytes(manifest))
    request_data = fixture["request"]
    request_data["market"]["configuration_json"] = json.dumps(fixture["market_profile"])
    return snapshot, request_data


def run(fixture, expected, paths, dependencies):
    # A frozen fallback import must never pass as the candidate wheel.
    import tivelo_backtest
    import tivelo_data
    from tivelo_backtest.archives import read_result
    from tivelo_backtest.runner import RunRequest, execute_run, prepare_run
    from tivelo_backtest.runtime import capture_runtime

    for module, name in ((tivelo_data, ENGINES[0]), (tivelo_backtest, ENGINES[1])):
        if (
            module.__version__ != expected[name].version
            or metadata.version(name) != expected[name].version
            or not Path(module.__file__).resolve().is_relative_to(paths[name])
        ):
            raise ValueError("实际载入的引擎与候选版本不符")
    runtime = capture_runtime(
        runtime_requirements(legacy_cpu="polars-runtime-compat" in dependencies)
    )
    if {p.name: p.version for p in runtime.packages} != {
        **dependencies,
        **{k: w.version for k, w in expected.items()},
    }:
        raise ValueError("候选引擎的依赖超出认证运行环境")
    results = {}
    with tempfile.TemporaryDirectory(prefix="tivelo-engine-check-") as temporary:
        root = Path(temporary)
        snapshot, request_data = synthetic_inputs(root, fixture)
        portable, definition = root / "snapshot", root / "definition"
        for mode in ("qfq", "hfq"):
            request_data["price_view"].update(mode=mode, provider_id=f"tivelo.{mode}")
            request = RunRequest.model_validate(request_data)
            archive, result = root / ("run-" + mode), root / ("result-" + mode)
            prepare_run(request, snapshot=portable, definition=definition, destination=archive)
            execute_run(run=archive, snapshot=portable, destination=result)
            lock, _, metrics = read_result(result)
            if (
                str(metrics.final_equity) != "5237.58"
                or metrics.filled_orders != 2
                or lock.contents.spec.snapshot_id != snapshot.snapshot_id
            ):
                raise ValueError("合成行情回测结果与发行基准不符")
            results[mode] = {
                "final_equity": str(metrics.final_equity),
                "filled_orders": metrics.filled_orders,
                "lock_id": lock.lock_id,
            }
        workbench = _workbench_replay(root, portable, root / "run-qfq", results["qfq"]["lock_id"])
    return {
        "datasets": len(snapshot.contents.datasets),
        "snapshot": snapshot.snapshot_id,
        "prices": results,
        "workbench": workbench,
        "runtime": runtime.reference.model_dump(mode="json"),
        "packages": [p.model_dump(mode="json") for p in runtime.packages],
    }
