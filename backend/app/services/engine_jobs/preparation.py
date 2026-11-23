"""Data and run preparation nodes, delegated to the independent engines."""

import os
import shutil
import tempfile
from dataclasses import dataclass

from tivelo_backtest.account import CashEquityAccount
from tivelo_backtest.archives import inspect_archive
from tivelo_backtest.definitions import DefinitionLoader
from tivelo_backtest.runner import run_requirements
from tivelo_backtest.runtime import CapturedRuntime, capture_runtime, verify_runtime
from tivelo_contracts.artifacts import SnapshotBundle
from tivelo_contracts.base import canonical_bytes, fingerprint
from tivelo_contracts.data import DatasetReference, DatasetVersion
from tivelo_contracts.errors import TiveloError
from tivelo_data import DataEngine
from tivelo_data.jsonl import JSONLinesProvider
from tivelo_data.native_source import NativeDataInput, NativeDataProvider, installation_credentials
from tivelo_data.providers import FixedSourceResolver, SyncContext
from tivelo_data.storage import Candidate, file_digest

from .preparation_models import (
    LockedCapture,
    LockedPreparation,
    LockedSource,
    PreparationReceipt,
    RepositoryDailyInput,
    RepositoryFactorInput,
    RepositoryMinuteInput,
    RoutedDailyInput,
    RoutedFactorInput,
    RoutedMinuteInput,
)
from .storage import JobError, JobStore

ROLES = frozenset({"sync", "publish", "snapshot", "prepare"})


def engine(store, resolver=None):
    return DataEngine(store.path("data"), resolver)


def source_provider(store, source, *, pin=None):
    if isinstance(source, NativeDataInput):
        return NativeDataProvider(store.data_dir, source, pin=pin)
    if isinstance(source, RepositoryMinuteInput):
        from tivelo_data.repository_minute import RepositoryMinuteProvider

        return RepositoryMinuteProvider(store.data_dir, source)
    if isinstance(source, RepositoryFactorInput):
        from tivelo_data.repository_factors import RepositoryFactorProvider

        return RepositoryFactorProvider(store.data_dir, source)
    if isinstance(source, RepositoryDailyInput):
        from tivelo_data.repository_daily import RepositoryDailyProvider

        return RepositoryDailyProvider(store.data_dir, source)
    if isinstance(source, RoutedDailyInput):
        from .routed_daily import open_daily_source

        return open_daily_source(store, source)
    if isinstance(source, RoutedMinuteInput):
        from .routed_daily import open_minute_source

        return open_minute_source(store, source)
    if isinstance(source, RoutedFactorInput):
        from .routed_daily import open_factor_source

        return open_factor_source(store, source)
    return JSONLinesProvider(
        store.path("inputs/" + source.path),
        source.request,
        source_id=source.source_id,
        knowledge_basis=source.knowledge_basis,
        bar_convention_id=source.bar_convention_id,
        coverage=source.coverage,
    )


@dataclass(frozen=True)
class _JobSourceResolver:
    store: JobStore
    locked: LockedSource

    def resolve(self, request):
        # The engine opens the provider after its cancellation check and owns its cleanup.
        provider = source_provider(self.store, self.locked.source, pin=self.locked.plan.extension)
        return FixedSourceResolver(provider, provider.configuration_digest).resolve(request)


def definitions_directory(store, plan_id, node_id):
    return store.path(f"jobs/{plan_id}/inputs/{node_id}/definitions")


def checked_definitions(store, locked, *, plan_id=None):
    request = locked.request.prepare
    if plan_id is None:
        return DefinitionLoader(
            store.path("inputs/" + request.dependency_root) if request.dependency_root else None
        ).inspect(
            store.path("inputs/" + request.definition),
            parameters_json=request.run.strategy_parameters_json,
        )
    directory = definitions_directory(store, plan_id, locked.request.node_id)
    definitions = DefinitionLoader(directory).inspect(
        directory / locked.preparation.definition.sha256, expected=locked.preparation.definition
    )
    if definitions.dependencies != locked.preparation.dependencies:
        raise JobError("DATA_CONFLICT", "保存的定义依赖与研究计划不一致")
    return definitions


def check_pinned_versions(store, request):
    specification = request.prepare or request.capture
    if not specification.versions:
        return
    data = engine(store)
    for reference in specification.versions:
        version = data.store.dataset_version(reference.version_id)
        if version.contents.dataset_id != reference.dataset_id:
            raise JobError("DATA_CONFLICT", "锁定版本属于其他数据集")
        if request.prepare and version.contents.knowledge_basis != "historical_versions":
            raise JobError("FUTURE_INFORMATION", "严格历史回测需要历史版本")


def snapshot_references(store, plan, locked):
    specification = locked.request.prepare or locked.request.capture
    references = list(specification.versions)
    for index in range(len(locked.preparation.sources)):
        parent = f"{locked.request.node_id}.publish_{index:02d}"
        version = DatasetVersion.model_validate(parent_payload(store, plan, parent))
        references.append(
            DatasetReference(dataset_id=version.contents.dataset_id, version_id=version.version_id)
        )
    return tuple(sorted(references, key=lambda reference: reference.dataset_id))


def freeze(store, request):
    specification = request.prepare or request.capture
    check_pinned_versions(store, request)
    required = {"tivelo-data"}
    pin = None
    extension_requirements = ()
    sources = []
    for source in specification.datasets:
        if request.prepare is not None and isinstance(
            source,
            (RepositoryDailyInput, RoutedDailyInput, RoutedFactorInput, RoutedMinuteInput, RepositoryFactorInput, RepositoryMinuteInput),
        ):
            raise JobError("FUTURE_INFORMATION", "该数据源只保存当前观察，严格历史回测需要历史版本")
        if isinstance(source, (RepositoryDailyInput, RoutedDailyInput, RepositoryMinuteInput)):
            required.add("tivelo-market-cn")
        if isinstance(source, RoutedMinuteInput):
            required.update(("tivelo-market-cn", "httpx", "PyYAML"))
        if isinstance(source, (RoutedDailyInput, RoutedFactorInput)):
            dependencies = {
                "tickflow": ("httpx", "tickflow"),
                "fuyao": ("httpx", "tivelo-market-cn"),
                "tdxquant": ("tdxaidata", "tivelo-market-cn"),
            }
            required.update(dependencies.get(source.expected_provider, ("httpx", "PyYAML")))
        if isinstance(source, NativeDataInput):
            required.add("tivelo-data[extensions]")
        provider = source_provider(store, source)
        try:
            resolver = FixedSourceResolver(provider, provider.configuration_digest)
            plan = engine(store, resolver).plan_sync(source.request)
            if (
                request.prepare is not None and (isinstance(source, NativeDataInput) or source.request.extension_field)
                and plan.capability.knowledge_basis != "historical_versions"
            ):
                raise JobError("FUTURE_INFORMATION", "严格历史回测需要来源声明历史版本")
            sources.append(LockedSource(source=source, plan=plan))
        finally:
            provider.close()
    if request.prepare is not None:
        definitions = DefinitionLoader(
            store.path("inputs/" + specification.dependency_root)
            if specification.dependency_root
            else None
        ).inspect(
            store.path("inputs/" + specification.definition),
            parameters_json=specification.run.strategy_parameters_json,
        )
        if specification.run.price_view.mode == "extension":
            from .price_views import JobPriceViews

            views = JobPriceViews(store.data_dir, 1)
            pin = views.resolve(specification.run.price_view.provider_id)
            extension_requirements = views.software_requirements
        required.update(
            run_requirements(definitions, specification.run.market, extension_requirements)
        )
    requirements = tuple(sorted(required))
    runtime = capture_runtime(requirements)
    values = dict(
        sources=tuple(sources),
        requirements=requirements,
        packages=runtime.packages,
        runtime=runtime.reference,
    )
    if request.capture is not None:
        return LockedCapture(**values)
    return LockedPreparation(
        **values,
        definition=definitions.root,
        dependencies=definitions.dependencies,
        price_extension=pin,
        extension_requirements=extension_requirements,
    )


def persist_definitions(store, plan):
    """Capture verified definition bytes before publishing the queued plan."""
    for locked in plan.inputs:
        if locked.request.prepare is None:
            continue
        destination = definitions_directory(store, plan.plan_id, locked.request.node_id)
        if destination.exists():
            checked_definitions(store, locked, plan_id=plan.plan_id)
            continue
        definitions = checked_definitions(store, locked)
        if (definitions.root, definitions.dependencies) != (
            locked.preparation.definition,
            locked.preparation.dependencies,
        ):
            raise JobError("DATA_CONFLICT", "策略在提交过程中改变")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.mkdtemp(prefix=".definitions-", dir=destination.parent)
        try:
            from pathlib import Path

            for node in definitions.nodes:
                prefix = Path(temporary) / node.bundle.bundle_id
                for relative, payload in (
                    ("definition.json", canonical_bytes(node.bundle)),
                    *node.files,
                ):
                    path = prefix / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as output:
                        output.write(payload)
                        output.flush()
                        os.fsync(output.fileno())
            if os.name != "nt":
                directories = [
                    Path(temporary),
                    *(path for path in Path(temporary).rglob("*") if path.is_dir()),
                ]
                for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
                    descriptor = os.open(path, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            os.rename(temporary, destination)
            if os.name != "nt":
                descriptor = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if os.path.exists(temporary):
                shutil.rmtree(temporary)
        checked_definitions(store, locked, plan_id=plan.plan_id)


def receipt(store, plan, node_id, attempt):
    from .artifacts import read_document

    path = store.attempt_directory(plan.plan_id, node_id, attempt["number"]) / "receipt.json"
    value = PreparationReceipt.model_validate_json(
        read_document(store.path(path.relative_to(store.root).as_posix()))
    )
    if value.plan_id != plan.plan_id or value.node_id != node_id:
        raise JobError("DATA_CONFLICT", "数据节点制品不属于当前计划")
    return value, path


def parent_payload(store, plan, node_id):
    from .artifacts import read_document, verify_completed

    artifact = verify_completed(store, plan, node_id)
    if artifact.kind == "dataset":
        return DatasetVersion.model_validate_json(
            read_document(store.path(artifact.path))
        ).model_dump(mode="json")
    state = store.get(plan.plan_id)
    return receipt(store, plan, node_id, state["nodes"][node_id]["attempts"][-1])[
        0
    ].payload.model_dump(mode="json")


def prepared_paths(store, plan, locked):
    from .artifacts import verify_completed

    prefix = locked.request.node_id
    run = verify_completed(store, plan, prefix + ".prepare")
    snapshot = verify_completed(store, plan, prefix + ".snapshot")
    return store.path(run.path), store.path(snapshot.path)


def release_export_reference(store, plan, node_id, artifact):
    if artifact.kind != "snapshot":
        return
    from .artifacts import read_document
    from .models import stages

    _, locked, _ = stages(plan)[node_id]
    bundle = SnapshotBundle.model_validate_json(
        read_document(store.path(artifact.path + "/bundle.json"))
    )
    engine(store).store.release(
        bundle.snapshot.snapshot_id, f"engine-job:{plan.plan_id}:{locked.request.node_id}"
    )


def inspect_node(store, plan, node_id, attempt):
    from .artifacts import read_document
    from .models import ArtifactReference, stages

    role, locked, parents = stages(plan)[node_id]
    directory = store.attempt_directory(plan.plan_id, node_id, attempt["number"])
    if role in {"sync", "publish"}:
        index = int(node_id.rsplit("_", 1)[1])
        source = locked.preparation.sources[index]
        if role == "sync":
            value, path = receipt(store, plan, node_id, attempt)
            candidate = Candidate.model_validate(value.payload)
            version = candidate.version
            engine(store).store.validate_candidate_files(candidate)
            kind, identity = "source", fingerprint(value)
        else:
            expected = Candidate.model_validate(parent_payload(store, plan, parents[0])).version
            data = engine(store)
            version = data.store.dataset_version(expected.version_id)
            if version != expected:
                raise JobError("DATA_CONFLICT", "发布版本与同步候选不一致")
            for artifact in version.contents.artifacts:
                file = engine(store).store.path(artifact.path)
                if (
                    file.stat().st_size != artifact.size_bytes
                    or file_digest(file) != artifact.sha256
                ):
                    raise JobError("ARTIFACT_CORRUPT", "已发布事实已改变")
            path = data.store.path(f"dataset_versions/{version.version_id}/manifest.json")
            kind, identity = "dataset", version.version_id
        if (
            version.contents.source_selection_digest != source.plan.plan_id
            or version.contents.dataset_id != source.source.request.dataset_id
        ):
            raise JobError("DATA_CONFLICT", "数据版本的来源与锁定计划不一致")
    elif role == "snapshot":
        path = store.path((directory / "output").relative_to(store.root).as_posix())
        bundle = SnapshotBundle.model_validate_json(
            read_document(store.path((path / "bundle.json").relative_to(store.root).as_posix()))
        )
        if bundle.snapshot.contents.datasets != snapshot_references(store, plan, locked):
            raise JobError("DATA_CONFLICT", "快照与已发布数据集不一致")
        for dataset in bundle.datasets:
            for artifact in dataset.contents.artifacts:
                file = store.path((path / artifact.path).relative_to(store.root).as_posix())
                if (
                    file.stat().st_size != artifact.size_bytes
                    or file_digest(file) != artifact.sha256
                ):
                    raise JobError("ARTIFACT_CORRUPT", "导出的快照事实已改变")
        kind, identity = "snapshot", fingerprint(bundle)
    else:
        path = store.path((directory / "output").relative_to(store.root).as_posix())
        archive, lock = inspect_archive(path)
        request, spec = locked.request.prepare.run, lock.contents.spec
        account = CashEquityAccount(
            request.account.currency,
            request.initial_capital,
            order_expiry=request.account.order_expiry,
            dividend_treatment=request.account.dividend_treatment,
        )
        from .artifacts import verify_completed

        snapshot = verify_completed(store, plan, parents[0])
        bundle = SnapshotBundle.model_validate_json(
            read_document(store.path(snapshot.path + "/bundle.json"))
        )
        if (
            spec.strategy != locked.preparation.definition
            or spec.dependencies != locked.preparation.dependencies
            or lock.contents.extensions
            != ((locked.preparation.price_extension,) if locked.preparation.price_extension else ())
            or lock.contents.extension_requirements != locked.preparation.extension_requirements
            or lock.contents.snapshot != bundle.snapshot
            or lock.contents.market_adapter != request.market
            or spec.account_profile != account.reference
            or spec.base_currency != request.account.currency
            or spec.semantics_version != request.semantics_version
            or spec.accounting_model != "event_ledger"
            or lock.contents.runtime is None
            or lock.contents.runtime.model_dump(exclude={"dependency_lock_sha256"})
            != locked.preparation.runtime.model_dump(exclude={"dependency_lock_sha256"})
            or any(
                getattr(spec, field) != getattr(request, field)
                for field in (
                    "universe",
                    "start",
                    "end",
                    "warmup_start",
                    "initial_capital",
                    "price_view",
                    "execution",
                    "extension_fields",
                )
            )
            or not set(lock.contents.packages).issubset(set(locked.preparation.packages))
        ):
            raise JobError("DATA_CONFLICT", "准备结果与研究请求不一致")
        kind, identity = "preparation", archive.archive_id
    return ArtifactReference(
        kind=kind, identity=identity, path=path.relative_to(store.root).as_posix()
    )


def execute_node(store, plan, node_id, attempt, observer):
    from .artifacts import publish_document, verify_completed
    from .models import stages

    role, locked, parents = stages(plan)[node_id]
    for parent in parents:
        verify_completed(store, plan, parent)
    prepared = locked.preparation
    verify_runtime(CapturedRuntime(prepared.packages, prepared.runtime), prepared.requirements)
    if observer.cancelled():
        raise JobError("CANCELLED", "输入准备已取消")
    directory = store.attempt_directory(plan.plan_id, node_id, attempt["number"])
    data = engine(store)
    payload = None
    if role == "sync":
        source = prepared.sources[int(node_id.rsplit("_", 1)[1])]
        data.resolver = _JobSourceResolver(store, source)

        def no_credentials(reference):
            raise JobError("AUTHENTICATION_FAILED", "本地文件来源不接受凭据")

        context = SyncContext(
            installation_credentials if isinstance(source.source, NativeDataInput) else no_credentials,
            observer.cancelled,
            lambda count, phase: observer.progress(count, 0, phase),
        )
        try:
            candidate = data.sync(source.plan, context)
        except TiveloError as exc:
            if source.source.kind in {"tsp_repository_daily", "tsp_repository_factors"} and getattr(
                exc, "issues", ()
            ):
                from tivelo_data.repository_diagnostics import RepositorySourceError

                if isinstance(exc, RepositorySourceError):
                    store.record_source_failure(plan.plan_id, node_id, attempt["token"], exc)
            raise
        payload = candidate.model_dump(mode="json")
    elif role == "publish":
        candidate = Candidate.model_validate(parent_payload(store, plan, parents[0]))
        data.validate(candidate)
        if observer.cancelled():
            raise JobError("CANCELLED", "数据发布已取消")
        data.publish(candidate)
    elif role == "snapshot":
        references = snapshot_references(store, plan, locked)
        snapshot = data.create_snapshot(tuple(reference.version_id for reference in references))
        if snapshot.contents.datasets != references:
            raise JobError("DATA_CONFLICT", "快照与锁定的数据版本不一致")
        data.export_snapshot(snapshot.snapshot_id, directory / "output", observer=observer)
    else:
        from tivelo_backtest.runner import prepare_run

        checked_definitions(store, locked, plan_id=plan.plan_id)
        definitions = definitions_directory(store, plan.plan_id, locked.request.node_id)
        snapshot = verify_completed(store, plan, parents[0])
        from .price_views import JobPriceViews

        prepare_run(
            locked.request.prepare.run,
            snapshot=store.path(snapshot.path),
            definition=definitions / prepared.definition.sha256,
            dependency_root=definitions,
            destination=directory / "output",
            observer=observer,
            extension_runtime=JobPriceViews(
                store.data_dir, plan.budget.timeout_seconds, required=prepared.price_extension
            ),
        )
    if payload is not None:
        value = PreparationReceipt(plan_id=plan.plan_id, node_id=node_id, payload=payload)
        publish_document(store, directory / "receipt.json", value, observer)
    return inspect_node(store, plan, node_id, attempt)
