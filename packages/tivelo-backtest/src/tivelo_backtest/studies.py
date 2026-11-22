"""Serial locked grid search and walk-forward evaluation over shared facts.

Each candidate/window gets a fresh strategy and account. Ranking consumes only
training metrics; the selected candidate alone is evaluated out of sample.
This reference workflow does not import the TSP optimizer or a concrete market.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path

from pydantic import TypeAdapter
from tivelo_contracts.base import (
    ExactDecimal,
    canonical_bytes,
    canonical_json,
    fingerprint,
    parse_json,
)
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import RunLock

from .archives import (
    MAX_MANIFEST_BYTES,
    _cancel,
    _durable_mkdir,
    _publish,
    _read,
    _record,
    _write,
    _write_inputs,
    calculate_metrics,
    publish_result,
    read_run,
)
from .controlled import ControlledProgram, derive_program_bundle, load_controlled
from .daily import DailyBacktest
from .definitions import CheckedDefinition, CheckedDefinitions, DefinitionLoader
from .market_loading import discover_market_adapter
from .price_views import open_price_view
from .runner import run_requirements
from .runtime import CapturedRuntime, verify_runtime
from .snapshot import PortableSnapshotReader
from .study_models import (
    StudyArchive,
    StudyArchiveContents,
    StudyCandidate,
    StudyPlan,
    StudyReport,
    StudyRequest,
    StudyTrial,
    StudyWindow,
    WindowResult,
)

_PROGRAM_AXES = frozenset(
    {"entry", "exit", "score", "max_positions", "weight_per_position", "min_history_bars"}
)
_EXACT = TypeAdapter(ExactDecimal)


def derive_candidate_payload(bundle, reference, overrides, *, program=None):
    """Pure artifact derivation, also usable when inspecting results without runtime dependencies."""
    changes = parse_json(overrides)
    if program is None:
        parameters = canonical_json(
            json.dumps({**parse_json(reference.parameters_json), **changes})
        )
        return bundle, bundle.reference(parameters), None
    derived = ControlledProgram.model_validate({**program.model_dump(), **changes})
    return derive_program_bundle(bundle, derived, version_prefix="study")


def derive_candidates(base, definitions, request, *, observer=None):
    """Derive content-bound candidates without constructing any trusted Python."""
    DefinitionLoader().validate(definitions)
    if (
        definitions.root != base.contents.spec.strategy
        or definitions.dependencies != base.contents.spec.dependencies
    ):
        raise TiveloError(
            ErrorCode.DEFINITION_CONFLICT, "Study definitions differ from its base RunLock"
        )
    node = definitions.nodes[-1]
    controlled = node.bundle.contents.language == "controlled_daily_v1"
    if controlled != (request.search.target == "controlled_program"):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Search target differs from the definition language"
        )
    if controlled and any(axis.name not in _PROGRAM_AXES for axis in request.search.axes):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Search axis is not a controlled program field"
        )
    compiled = load_controlled(definitions).implementation if controlled else None
    if base.contents.scale_proof != (compiled.proof if compiled is not None else None):
        raise TiveloError(ErrorCode.PRICE_BASIS_UNPROVEN, "Study base has an invalid scale proof")
    template = compiled.program if compiled is not None else None
    candidates, graphs = [], {}
    for overrides in request.search.expand(request.limits.max_candidates):
        _cancel(observer)
        bundle, root, payload = derive_candidate_payload(
            node.bundle, node.reference, overrides, program=template
        )
        if controlled:
            graph = CheckedDefinitions(
                root, (CheckedDefinition(bundle, root, ((bundle.contents.entrypoint, payload),)),)
            )
            # Validate all programs before the first candidate executes.
            load_controlled(graph)
        else:
            graph = CheckedDefinitions(
                root, (*definitions.nodes[:-1], replace(node, reference=root))
            )
            DefinitionLoader().validate(graph)
        identity = fingerprint(root)
        if identity in graphs:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Grid contains equivalent normalized candidates"
            )
        graphs[identity] = graph
        candidates.append(
            StudyCandidate(candidate_id=identity, overrides_json=overrides, strategy=root)
        )
    return tuple(sorted(candidates, key=lambda candidate: candidate.candidate_id)), graphs


def _windows(base, request, active, all_days):
    spec = base.contents.spec
    delay = timedelta(seconds=spec.execution.decision_delay_seconds)
    if request.mode == "grid":
        return (
            StudyWindow(
                index=0,
                train_start=spec.start,
                train_end=spec.end,
                train_warmup_start=spec.warmup_start,
                selection_at=active[-1].close_at + delay,
            ),
        )
    settings = request.walk_forward
    indexes = {day.trading_date: index for index, day in enumerate(all_days)}

    def warmup(day):
        if not settings.warmup_sessions:
            return None
        index = indexes[day.trading_date] - settings.warmup_sessions
        if index < 0:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Base run does not include requested warmup sessions",
            )
        return all_days[index].trading_date

    result, offset = [], 0
    candidate_count = len(request.search.expand(request.limits.max_candidates))
    while True:
        train_start = 0 if settings.training == "expanding" else offset
        train_end = offset + settings.train_sessions - 1
        test_start = train_end + settings.gap_sessions + 1
        test_end = test_start + settings.test_sessions - 1
        if test_end >= len(active):
            break
        if (len(result) + 1) * (candidate_count + 1) > request.limits.max_executions:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Walk-forward exceeds its execution budget"
            )
        result.append(
            StudyWindow(
                index=len(result),
                train_start=active[train_start].trading_date,
                train_end=active[train_end].trading_date,
                train_warmup_start=warmup(active[train_start]),
                selection_at=active[train_end].close_at + delay,
                test_start=active[test_start].trading_date,
                test_end=active[test_end].trading_date,
                test_warmup_start=warmup(active[test_start]),
                test_open_at=active[test_start].open_at,
            )
        )
        offset += settings.step_sessions
    if not result:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT,
            "No complete walk-forward window fits the locked range",
        )
    return tuple(result)


def trial_lock(base, graph, window, *, test=False):
    prefix = "test" if test else "train"
    spec = base.contents.spec.model_copy(
        update={
            "strategy": graph.root,
            "dependencies": graph.dependencies,
            "start": getattr(window, prefix + "_start"),
            "end": getattr(window, prefix + "_end"),
            "warmup_start": getattr(window, prefix + "_warmup_start"),
        }
    )
    controlled = graph.nodes[-1].bundle.contents.language == "controlled_daily_v1"
    proof = load_controlled(graph).implementation.proof if controlled else None
    return RunLock.create(base.contents.model_copy(update={"spec": spec, "scale_proof": proof}))


def prepare_study(
    base, definitions, request, *, rules, reader, observer=None, extension_runtime=None
):
    """Resolve finite candidates and real calendar windows without running Python."""
    request = StudyRequest.model_validate_json(canonical_bytes(request))
    base = RunLock.model_validate_json(canonical_bytes(base))
    if getattr(reader, "prepared_fact_bytes", 0) > request.limits.max_prepared_bytes:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Reader exceeds the prepared-fact byte budget"
        )
    _cancel(observer)
    candidates, graphs = derive_candidates(base, definitions, request, observer=observer)
    runner = DailyBacktest(rules=rules, price_view=open_price_view(base, extension_runtime))
    active = runner.preflight(
        base, reader, strategy=definitions.root, dependencies=definitions.dependencies
    )
    spec = base.contents.spec
    all_days = tuple(
        rules.trading_days(
            spec.warmup_start or spec.start,
            spec.end,
            as_of=datetime.combine(spec.end, time.max, UTC),
        )
    )
    plan = StudyPlan(
        base_lock_id=base.lock_id,
        request=request,
        candidates=candidates,
        calendar_days=all_days,
        windows=_windows(base, request, active, all_days),
    )
    if len(canonical_bytes(plan)) > MAX_MANIFEST_BYTES:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Study plan exceeds its document budget")
    count = len(plan.windows) * (len(candidates) + (request.mode == "walk_forward"))
    # Every result is independently readable, including its definition closure.
    maximum_files = max(
        sum(len(node.files) + 1 for node in graph.nodes) + 8 for graph in graphs.values()
    )
    base_files = sum(len(node.files) + 1 for node in definitions.nodes) + 3
    if count * maximum_files + base_files > 8192:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Study would exceed its archive file budget")
    for window in plan.windows:
        for graph in graphs.values():
            _cancel(observer)
            for test in (False, True) if request.mode == "walk_forward" else (False,):
                lock = trial_lock(base, graph, window, test=test)
                if spec.price_view.mode == "extension":
                    runner = DailyBacktest(
                        rules=rules, price_view=open_price_view(lock, extension_runtime)
                    )
                days = runner.preflight(
                    lock, reader, strategy=graph.root, dependencies=graph.dependencies
                )
                if lock.contents.scale_proof is None or spec.price_view.mode == "extension":
                    runner.price_view.project(
                        (),
                        (),
                        spec.price_view,
                        decision_at=days[0].close_at
                        + timedelta(seconds=spec.execution.decision_delay_seconds),
                    )
    return plan, graphs


def select_winner(training, objective):
    eligible = [
        trial for trial in training if trial.metrics.filled_orders >= objective.min_filled_orders
    ]
    if not eligible:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID,
            "No training candidate meets the minimum filled-order requirement",
        )

    def key(trial):
        value = getattr(trial.metrics, objective.metric)
        return (value if objective.direction == "min" else value.copy_negate(), trial.candidate_id)

    return min(eligible, key=key).candidate_id


def compounded_oos(windows):
    if not windows or windows[0].test is None:
        return None
    try:
        with localcontext(Context(prec=96)):
            equity = Decimal(1)
            for window in windows:
                # All folds, including losses and zero-trade tests, participate.
                equity *= 1 + window.test.metrics.total_return
            return _EXACT.validate_python(
                (equity - 1).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
            )
    except (InvalidOperation, ValueError) as exc:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "OOS aggregate exceeds decimal128 precision"
        ) from exc


class _TrialObserver:
    def __init__(self, parent, label):
        self.parent, self.label = parent, label

    def cancelled(self):
        return self.parent.cancelled()

    def progress(self, done, total, phase):
        self.parent.progress(done, total, self.label + ":" + phase)


def evaluate_study(
    base, definitions, request, *, rules, reader, destination, observer=None, extension_runtime=None
):
    """Library reference workflow. Caller owns reader/runtime lifecycle, as with DailyBacktest.

    CLI users should use run_study(), which verifies the installed environment and
    prepares one shared portable fact store. No successful root archive is published
    until every requested trial and OOS result has been verified.
    """
    _cancel(observer)
    base = RunLock.model_validate_json(canonical_bytes(base))
    plan, graphs = prepare_study(
        base,
        definitions,
        request,
        rules=rules,
        reader=reader,
        observer=observer,
        extension_runtime=extension_runtime,
    )
    return _evaluate_prepared(
        base,
        definitions,
        plan,
        graphs,
        rules=rules,
        reader=reader,
        destination=destination,
        observer=observer,
        extension_runtime=extension_runtime,
    )


def _evaluate_prepared(
    base,
    definitions,
    plan,
    graphs,
    *,
    rules,
    reader,
    destination,
    observer=None,
    extension_runtime=None,
    _before_commit=None,
    _expected_archive_id=None,
):
    request = plan.request
    requested = Path(destination).expanduser().absolute()
    target = requested.parent.resolve() / requested.name
    from .study_archives import inspect_study, read_study

    if target.exists() or target.is_symlink():
        manifest, existing_plan, _ = read_study(target)
        if existing_plan != plan:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Destination contains a different study")
        if _expected_archive_id is not None and manifest.archive_id != _expected_archive_id:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Replay destination differs from the original study"
            )
        if _before_commit is not None:
            _before_commit()
        return manifest
    try:
        _durable_mkdir(target.parent)
        with tempfile.TemporaryDirectory(prefix=".tivelo-study-", dir=target.parent) as temporary:
            stage = Path(temporary)
            paths = _write_inputs(stage, base, definitions)
            _write(stage, "study-plan.json", canonical_bytes(plan))
            paths.append("study-plan.json")
            inventory = [_record(stage, path) for path in paths]
            if sum(record.size_bytes for record in inventory) > request.limits.max_archive_bytes:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Study inputs exceed the archive byte budget"
                )
            windows = []
            completed = 0
            total = len(plan.windows) * (len(plan.candidates) + (request.mode == "walk_forward"))

            def execute(candidate, window, test=False):
                nonlocal completed
                _cancel(observer)
                graph = graphs[candidate.candidate_id]
                lock = trial_lock(base, graph, window, test=test)
                role = "test" if test else "train"
                relative = f"trials/{window.index:03d}/{role}/{candidate.candidate_id}"
                trial_observer = (
                    _TrialObserver(
                        observer, f"study:{window.index}:{role}:{candidate.candidate_id}"
                    )
                    if observer is not None
                    else None
                )
                runner = DailyBacktest(
                    rules=rules, price_view=open_price_view(lock, extension_runtime)
                )
                if graph.nodes[-1].bundle.contents.language == "controlled_daily_v1":
                    result = runner.run(
                        lock, reader, load_controlled(graph), observer=trial_observer
                    )
                else:
                    with DefinitionLoader().load_trusted_python(graph) as binding:
                        result = runner.run(lock, reader, binding, observer=trial_observer)
                metrics = calculate_metrics(lock, result)
                archive = publish_result(
                    lock, graph, result, stage / relative, observer=trial_observer
                )
                for record in archive.contents.files:
                    inventory.append(
                        record.model_copy(update={"path": relative + "/" + record.path})
                    )
                inventory.append(_record(stage, relative + "/manifest.json"))
                if (
                    sum(record.size_bytes for record in inventory)
                    > request.limits.max_archive_bytes
                ):
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Study exceeds its archive byte budget"
                    )
                completed += 1
                if observer is not None:
                    observer.progress(completed, total, "study_trials")
                return StudyTrial(
                    candidate_id=candidate.candidate_id,
                    lock_id=lock.lock_id,
                    result_archive_id=archive.archive_id,
                    path=relative,
                    metrics=metrics,
                )

            for window in plan.windows:
                training = tuple(execute(candidate, window) for candidate in plan.candidates)
                winner = select_winner(training, request.objective)
                selected = next(
                    candidate for candidate in plan.candidates if candidate.candidate_id == winner
                )
                test = (
                    execute(selected, window, test=True) if request.mode == "walk_forward" else None
                )
                windows.append(
                    WindowResult(
                        index=window.index, training=training, winner_candidate_id=winner, test=test
                    )
                )
            report = StudyReport(
                study_id=plan.study_id,
                windows=tuple(windows),
                aggregate_method="independent_fold_return_product"
                if request.mode == "walk_forward"
                else "none",
                oos_compounded_return=compounded_oos(windows),
                prepared_fact_bytes=getattr(reader, "prepared_fact_bytes", 0),
            )
            _write(stage, "study-report.json", canonical_bytes(report))
            inventory.append(_record(stage, "study-report.json"))
            manifest = StudyArchive.create(
                StudyArchiveContents(
                    study_id=plan.study_id,
                    lock_id=base.lock_id,
                    files=tuple(sorted(inventory, key=lambda record: record.path)),
                )
            )
            if sum(record.size_bytes for record in inventory) > request.limits.max_archive_bytes:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Study exceeds its archive byte budget"
                )
            _cancel(observer)
            if _expected_archive_id is not None and manifest.archive_id != _expected_archive_id:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Study replay did not reproduce the original archive"
                )
            if _before_commit is not None:
                _before_commit()
            return _publish(stage, target, manifest, observer, inspect_existing=inspect_study)
    except (OSError, ValueError, TypeError) as exc:
        raise TiveloError(
            ErrorCode.PUBLICATION_FAILED, "Study construction or publication failed"
        ) from exc


def run_study(
    request: StudyRequest, *, run, snapshot, destination, observer=None, extension_runtime=None
):
    """Execute archived inputs in the verified current environment; no installation."""
    request = StudyRequest.model_validate_json(canonical_bytes(request))
    _cancel(observer)
    base, definitions = read_run(run)
    return _run_loaded_study(
        request,
        base,
        definitions,
        snapshot=snapshot,
        destination=destination,
        observer=observer,
        extension_runtime=extension_runtime,
    )


def replay_study(*, archive, snapshot, destination, observer=None, extension_runtime=None):
    """Recompute an archived study, rejecting mismatches before publishing a result."""
    from .study_archives import read_study

    _cancel(observer)
    manifest, plan, _ = read_study(archive)
    root = Path(archive).expanduser().absolute()
    base = RunLock.model_validate_json(_read(root, "run-lock.json"))
    definitions = DefinitionLoader(root / "definitions").inspect(
        root / "definitions" / base.contents.spec.strategy.sha256,
        expected=base.contents.spec.strategy,
    )
    return _run_loaded_study(
        plan.request,
        base,
        definitions,
        snapshot=snapshot,
        destination=destination,
        observer=observer,
        expected_plan=plan,
        extension_runtime=extension_runtime,
        expected_archive_id=manifest.archive_id,
    )


def _run_loaded_study(
    request,
    base,
    definitions,
    *,
    snapshot,
    destination,
    observer=None,
    expected_plan=None,
    extension_runtime=None,
    expected_archive_id=None,
):
    adapter = base.contents.market_adapter
    if adapter is None:
        raise TiveloError(ErrorCode.RULES_UNAVAILABLE, "Study base has no market adapter")
    requirements = run_requirements(definitions, adapter, base.contents.extension_requirements)
    runtime = CapturedRuntime(base.contents.packages, base.contents.runtime)
    verify_runtime(runtime, requirements)
    rules = discover_market_adapter(adapter).load()
    with PortableSnapshotReader(snapshot, snapshot_id=base.contents.spec.snapshot_id) as reader:
        plan, graphs = prepare_study(
            base,
            definitions,
            request,
            rules=rules,
            reader=reader,
            observer=observer,
            extension_runtime=extension_runtime,
        )
        if expected_plan is not None and plan != expected_plan:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Replayed study plan differs from the archived plan"
            )
        reader.prepare(max_bytes=request.limits.max_prepared_bytes, observer=observer)

        def verify_inputs():
            reader.verify()
            verify_runtime(runtime, requirements)

        return _evaluate_prepared(
            base,
            definitions,
            plan,
            graphs,
            rules=rules,
            reader=reader,
            destination=destination,
            observer=observer,
            _before_commit=verify_inputs,
            extension_runtime=extension_runtime,
            _expected_archive_id=expected_archive_id,
        )
