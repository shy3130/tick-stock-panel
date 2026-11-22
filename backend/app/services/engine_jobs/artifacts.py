"""Content-bound research artifacts; no current preferences or implicit latest input."""

import hashlib

from tivelo_backtest.archives import inspect_archive, read_result, read_run
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_backtest.study_archives import inspect_study, read_study
from tivelo_backtest.study_models import StudyPlan
from tivelo_contracts.artifacts import RunArchive, SnapshotBundle
from tivelo_contracts.base import canonical_bytes, fingerprint
from tivelo_contracts.execution import RunLock

from app.services.fs_utils import atomic_write_text

from .models import (
    ArtifactReference,
    JobPlan,
    LockedInput,
    ResearchReport,
    ValidationReceipt,
    plan_format,
    stages,
)
from .storage import JobError


def read_document(path, limit=16 * 1024**2):
    if path.stat().st_size > limit:
        raise JobError("RESOURCE_LIMIT", "任务制品文档超过大小限制")
    with path.open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise JobError("RESOURCE_LIMIT", "任务制品文档超过大小限制")
    return content


def read_input_run(store, relative):
    """Read bounded run metadata without executing a strategy or reading fact tables."""
    archive = RunArchive.model_validate_json(
        read_document(store.path(f"inputs/{relative}/manifest.json"))
    )
    payload = read_document(store.path(f"inputs/{relative}/run-lock.json"))
    lock = RunLock.model_validate_json(payload)
    declaration = next(item for item in archive.contents.files if item.path == "run-lock.json")
    if declaration.sha256 != hashlib.sha256(payload).hexdigest() or declaration.size_bytes != len(
        payload
    ):
        raise JobError("ARTIFACT_CORRUPT", "基础任务锁与归档清单不一致")
    if archive.contents.lock_id != lock.lock_id:
        raise JobError("ARTIFACT_CORRUPT", "基础任务锁与归档身份不一致")
    return archive, lock


def lock_input(store, request, prior=None):
    if request.prepare is not None or request.capture is not None:
        from .preparation import freeze

        return LockedInput(
            request=request, preparation=prior.preparation if prior else freeze(store, request)
        )
    archive, lock = read_input_run(store, request.run)
    bundle = SnapshotBundle.model_validate_json(
        read_document(store.path(f"inputs/{request.snapshot}/bundle.json"))
    )
    if lock.contents.snapshot != bundle.snapshot:
        raise JobError("DATA_CONFLICT", "基础任务与选择的快照不一致")
    # Resolve only bounded metadata here. Fact validation runs in the worker.
    return LockedInput(
        request=request,
        run_archive_id=archive.archive_id,
        lock_id=lock.lock_id,
        snapshot_id=bundle.snapshot.snapshot_id,
        bundle_id=fingerprint(bundle),
    )


def freeze_request(store, request, *, budget=None, prior=None):
    previous = {item.request.node_id: item for item in prior.inputs} if prior else {}
    inputs = tuple(lock_input(store, node, previous.get(node.node_id)) for node in request.nodes)
    return JobPlan(
        plan_version=plan_format(inputs),
        request=request,
        inputs=inputs,
        effective_budget=budget,
    )


def input_paths(store, locked, plan=None):
    if locked.preparation is not None:
        from .preparation import prepared_paths

        return prepared_paths(store, plan, locked)
    return store.path("inputs/" + locked.request.run), store.path(
        "inputs/" + locked.request.snapshot
    )


def verify_input(store, locked, plan=None):
    if locked.preparation is None and lock_input(store, locked.request) != locked:
        raise JobError("DATA_CONFLICT", "任务输入已改变，请创建新的任务")
    run, snapshot = input_paths(store, locked, plan)
    archive, _ = inspect_archive(run)
    if locked.preparation is None and archive.archive_id != locked.run_archive_id:
        raise JobError("DATA_CONFLICT", "基础任务归档已改变")
    lock, _ = read_run(run)
    with PortableSnapshotReader(snapshot, snapshot_id=lock.contents.snapshot.snapshot_id) as reader:
        reader.verify()
    return lock


def _receipt(plan, node_id, locked):
    return ValidationReceipt(plan_id=plan.plan_id, node_id=node_id, input=locked)


def publish_document(store, path, value, observer):
    payload = canonical_bytes(value)
    if len(payload) > 4 * 1024**2:
        raise JobError("RESOURCE_LIMIT", "任务报告超过大小限制")
    if observer.cancelled():
        raise JobError("CANCELLED", "研究任务已取消")
    if path.exists():
        if read_document(path) != payload:
            raise JobError("DATA_CONFLICT", "目标已有不同的研究制品")
        return
    observer.progress(0, 1, "publishing")
    if observer.cancelled():
        raise JobError("CANCELLED", "研究任务已取消")
    atomic_write_text(path, payload.decode("utf-8"), durable=True)


def inspect_attempt(store, plan, node_id, attempt):
    """Bounded metadata/hash inspection in the parent; no DataFrame materialization."""
    role, locked, parents = stages(plan)[node_id]
    from .preparation import ROLES, inspect_node

    if role in ROLES:
        return inspect_node(store, plan, node_id, attempt)
    directory = store.attempt_directory(plan.plan_id, node_id, attempt["number"])
    if role == "execute":
        expected_lock_id = locked.lock_id
        if locked.preparation is not None:
            run, _ = input_paths(store, locked, plan)
            _, original_lock = inspect_archive(run)
            expected_lock_id = original_lock.lock_id
        output = store.path((directory / "output").relative_to(store.root).as_posix())
        if locked.request.study is None:
            archive, _ = inspect_archive(output, result=True)
            kind = "result"
        else:
            archive = inspect_study(output)
            study = StudyPlan.model_validate_json(
                read_document(
                    store.path((output / "study-plan.json").relative_to(store.root).as_posix())
                )
            )
            if (
                study.request != locked.request.study
                or study.base_lock_id != expected_lock_id
                or archive.contents.study_id != study.study_id
            ):
                raise JobError("DATA_CONFLICT", "研究制品的参数或窗口与锁定计划不一致")
            kind = "study"
        if archive.contents.lock_id != expected_lock_id:
            raise JobError("DATA_CONFLICT", "执行制品属于其他输入锁")
        return ArtifactReference(
            kind=kind, identity=archive.archive_id, path=output.relative_to(store.root).as_posix()
        )
    path = store.path(
        (directory / ("receipt.json" if role == "validate" else "report.json"))
        .relative_to(store.root)
        .as_posix()
    )
    if role == "validate":
        content = ValidationReceipt.model_validate_json(read_document(path))
        if content != _receipt(plan, node_id, locked):
            raise JobError("DATA_CONFLICT", "输入检查凭据与计划不一致")
        kind = "validation"
    else:
        content = ResearchReport.model_validate_json(read_document(path))
        state = store.get(plan.plan_id)
        execution = state["nodes"][parents[0]]["artifact"]
        if (
            content.plan_id != plan.plan_id
            or content.node_id != node_id
            or content.execution.model_dump(mode="json") != execution
        ):
            raise JobError("DATA_CONFLICT", "报告引用与执行结果不一致")
        kind = "report"
    return ArtifactReference(
        kind=kind, identity=fingerprint(content), path=path.relative_to(store.root).as_posix()
    )


def verify_completed(store, plan, node_id):
    node = store.get(plan.plan_id)["nodes"][node_id]
    if node["status"] != "succeeded" or not node["attempts"]:
        raise JobError("STATE_CONFLICT", "上游节点未完成")
    actual = inspect_attempt(store, plan, node_id, node["attempts"][-1])
    if actual.model_dump(mode="json") != node["artifact"]:
        raise JobError("ARTIFACT_CORRUPT", "已完成的上游制品已改变")
    return actual


def read_published_report(store, job_id, node_id):
    plan = store.plan(job_id)
    if node_id not in {item.request.node_id for item in plan.inputs if not item.request.capture}:
        raise JobError("NOT_FOUND", "研究节点不存在")
    archive = verify_completed(store, plan, node_id + ".report")
    report = ResearchReport.model_validate_json(read_document(store.path(archive.path)))
    if fingerprint(report) != archive.identity:
        raise JobError("ARTIFACT_CORRUPT", "已发布的研究报告已改变")
    return report


def execute_node(store, plan, node_id, attempt, observer):
    role, locked, parents = stages(plan)[node_id]
    from .preparation import ROLES

    if role in ROLES:
        from .preparation import execute_node as prepare_node

        return prepare_node(store, plan, node_id, attempt, observer)
    for parent in parents:
        verify_completed(store, plan, parent)
    directory = store.attempt_directory(plan.plan_id, node_id, attempt["number"])
    if role == "validate":
        verify_input(store, locked, plan)
        publish_document(
            store, directory / "receipt.json", _receipt(plan, node_id, locked), observer
        )
    elif role == "execute":
        verify_input(store, locked, plan)
        run, snapshot = input_paths(store, locked, plan)
        from .price_views import JobPriceViews

        arguments = dict(
            run=run,
            snapshot=snapshot,
            destination=directory / "output",
            observer=observer,
            extension_runtime=JobPriceViews(store.data_dir, plan.budget.timeout_seconds),
        )
        if locked.request.study is None:
            from tivelo_backtest.runner import execute_run

            execute_run(**arguments)
        else:
            from tivelo_backtest.studies import run_study

            run_study(locked.request.study, **arguments)
    else:
        execution = verify_completed(store, plan, parents[0])
        path = store.path(execution.path)
        if execution.kind == "study":
            _, study, report = read_study(path)
            summary = {
                "study": study.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            }
            lock = RunLock.model_validate_json(read_document(path / "run-lock.json"))
        else:
            lock, result, metrics = read_result(path)
            summary = {
                "metrics": metrics.model_dump(mode="json"),
                "diagnostics": list(result.diagnostics),
            }
        summary["semantics"] = {
            "start": lock.contents.spec.start.isoformat(),
            "end": lock.contents.spec.end.isoformat(),
            "universe_count": len(lock.contents.spec.universe),
            "semantics_version": lock.contents.spec.semantics_version,
            "lock_id": lock.lock_id,
            "strategy": lock.contents.spec.strategy.model_dump(mode="json"),
            "price_view": lock.contents.spec.price_view.model_dump(mode="json"),
            "accounting_model": lock.contents.spec.accounting_model,
            "execution": lock.contents.spec.execution.model_dump(mode="json"),
            "market_profile": lock.contents.spec.market_profile.model_dump(mode="json"),
            "snapshot_id": lock.contents.spec.snapshot_id,
            "knowledge_requirement": lock.contents.spec.knowledge_requirement,
        }
        report = ResearchReport(
            plan_id=plan.plan_id, node_id=node_id, execution=execution, summary=summary
        )
        publish_document(store, directory / "report.json", report, observer)
    return inspect_attempt(store, plan, node_id, attempt)
