"""Read and verify study results without executing archived definitions or rules."""

from datetime import timedelta
from pathlib import Path

from tivelo_contracts.artifacts import ResultArchive
from tivelo_contracts.base import fingerprint
from tivelo_contracts.definitions import DefinitionBundle
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import RunLock

from .archives import MAX_ARCHIVE_BYTES, _definition_inventory, _read, _record, read_result
from .controlled import ControlledProgram
from .study_models import StudyArchive, StudyCandidate, StudyPlan, StudyReport


def inspect_study(directory):
    root = Path(directory).expanduser().absolute()
    try:
        manifest = StudyArchive.model_validate_json(_read(root, "manifest.json"))
        if sum(item.size_bytes for item in manifest.contents.files) > MAX_ARCHIVE_BYTES:
            raise ValueError("study exceeds archive byte budget")
        for item in manifest.contents.files:
            if _record(root, item.path, item.row_count, expected_size=item.size_bytes) != item:
                raise ValueError("study file digest differs")
        return manifest
    except (ValueError, OSError, TypeError) as exc:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Study archive failed verification") from exc


def read_study(directory):
    """Return manifest, plan and recomputed summary; no market/license/runtime loading."""
    from .studies import (
        _PROGRAM_AXES,
        _windows,
        compounded_oos,
        derive_candidate_payload,
        select_winner,
    )

    root = Path(directory).expanduser().absolute()
    manifest = inspect_study(root)
    try:
        base = RunLock.model_validate_json(_read(root, "run-lock.json"))
        plan = StudyPlan.model_validate_json(_read(root, "study-plan.json"))
        report = StudyReport.model_validate_json(_read(root, "study-report.json"))
        if (
            manifest.contents.lock_id != base.lock_id
            or plan.base_lock_id != base.lock_id
            or manifest.contents.study_id != plan.study_id
            or report.study_id != plan.study_id
            or sum(item.size_bytes for item in manifest.contents.files)
            > plan.request.limits.max_archive_bytes
            or report.prepared_fact_bytes > plan.request.limits.max_prepared_bytes
        ):
            raise ValueError("study identities or budgets differ")
        inventory = {item.path: item for item in manifest.contents.files}
        bundle, expected = _definition_inventory(
            root, base.contents.spec.strategy, base.contents.spec.dependencies, inventory
        )
        expected.update(("run-lock.json", "study-plan.json", "study-report.json"))
        controlled = bundle.contents.language == "controlled_daily_v1"
        if controlled != (plan.request.search.target == "controlled_program"):
            raise ValueError("search target differs from base definition")
        program = None
        if controlled:
            if any(axis.name not in _PROGRAM_AXES for axis in plan.request.search.axes):
                raise ValueError("invalid controlled search axis")
            program = ControlledProgram.model_validate_json(
                _read(
                    root, f"definitions/{bundle.bundle_id}/{bundle.contents.entrypoint}", 1_048_576
                )
            )
            expected_proof = program.proof_for(base.contents.spec.strategy)
        else:
            expected_proof = None
        if base.contents.scale_proof != expected_proof:
            raise ValueError("base proof differs from archived program")
        candidates, candidate_bundles = [], {}
        for override in plan.request.search.expand(plan.request.limits.max_candidates):
            derived, reference, payload = derive_candidate_payload(
                bundle, base.contents.spec.strategy, override, program=program
            )
            identity = fingerprint(reference)
            candidates.append(
                StudyCandidate(candidate_id=identity, strategy=reference, overrides_json=override)
            )
            candidate_bundles[identity] = derived, payload
        if (
            tuple(sorted(candidates, key=lambda candidate: candidate.candidate_id))
            != plan.candidates
        ):
            raise ValueError("candidate derivation differs from locked search space")
        if len(report.windows) != len(plan.windows):
            raise ValueError("study window count differs")
        spec = base.contents.spec
        if any(
            not (spec.warmup_start or spec.start) <= day.trading_date <= spec.end
            for day in plan.calendar_days
        ):
            raise ValueError("study calendar exceeds the base range")
        active = tuple(day for day in plan.calendar_days if day.trading_date >= spec.start)
        if not active or _windows(base, plan.request, active, plan.calendar_days) != plan.windows:
            raise ValueError("study windows differ from the locked session boundaries")
        candidates = {candidate.candidate_id: candidate for candidate in plan.candidates}
        for window, outcome in zip(plan.windows, report.windows, strict=True):
            if outcome.index != window.index or [
                trial.candidate_id for trial in outcome.training
            ] != list(candidates):
                raise ValueError("training candidates are missing, duplicated or unordered")
            if (
                select_winner(outcome.training, plan.request.objective)
                != outcome.winner_candidate_id
            ):
                raise ValueError("winner differs from training-only ranking")
            if (outcome.test is not None) != (plan.request.mode == "walk_forward") or (
                outcome.test is not None
                and outcome.test.candidate_id != outcome.winner_candidate_id
            ):
                raise ValueError("test result is not the training-selected candidate")
            for test, trials in (
                (False, outcome.training),
                (True, () if outcome.test is None else (outcome.test,)),
            ):
                role = "test" if test else "train"
                for trial in trials:
                    candidate = candidates[trial.candidate_id]
                    path = f"trials/{window.index:03d}/{role}/{candidate.candidate_id}"
                    if trial.path != path:
                        raise ValueError("trial path differs")
                    child_manifest = ResultArchive.model_validate_json(
                        _read(root, path + "/manifest.json")
                    )
                    child_lock, result, metrics = read_result(root / path)
                    derived, payload = candidate_bundles[candidate.candidate_id]
                    proof = (
                        ControlledProgram.model_validate_json(payload).proof_for(candidate.strategy)
                        if controlled
                        else None
                    )
                    spec = base.contents.spec.model_copy(
                        update={
                            "strategy": candidate.strategy,
                            "start": getattr(window, role + "_start"),
                            "end": getattr(window, role + "_end"),
                            "warmup_start": getattr(window, role + "_warmup_start"),
                        }
                    )
                    expected_lock = RunLock.create(
                        base.contents.model_copy(update={"spec": spec, "scale_proof": proof})
                    )
                    if (
                        child_lock != expected_lock
                        or child_manifest.archive_id != trial.result_archive_id
                        or trial.lock_id != child_lock.lock_id
                        or metrics != trial.metrics
                    ):
                        raise ValueError("trial does not match its window, definition or metrics")
                    if not test and result.equity[-1].at != window.selection_at:
                        raise ValueError("training selection time differs from the final decision")
                    if test and result.equity[0].at <= window.selection_at:
                        raise ValueError("test results precede selection")
                    expected_points = tuple(
                        day.close_at + timedelta(seconds=spec.execution.decision_delay_seconds)
                        for day in plan.calendar_days
                        if getattr(window, role + "_start")
                        <= day.trading_date
                        <= getattr(window, role + "_end")
                    )
                    if tuple(point.at for point in result.equity) != expected_points:
                        raise ValueError("trial equity dates differ from the locked calendar")
                    child_bundle = DefinitionBundle.model_validate_json(
                        _read(
                            root,
                            f"{path}/definitions/{derived.bundle_id}/definition.json",
                            1_048_576,
                        )
                    )
                    if child_bundle != derived:
                        raise ValueError("trial definition does not derive from the base program")
                    expected.add(path + "/manifest.json")
                    for item in child_manifest.contents.files:
                        relative = path + "/" + item.path
                        if inventory.get(relative) != item.model_copy(update={"path": relative}):
                            raise ValueError("trial file differs from study inventory")
                        expected.add(relative)
        method = (
            "independent_fold_return_product" if plan.request.mode == "walk_forward" else "none"
        )
        if report.aggregate_method != method or report.oos_compounded_return != compounded_oos(
            report.windows
        ):
            raise ValueError("study aggregate differs from all OOS windows")
        if expected != set(inventory) or inspect_study(root) != manifest:
            raise ValueError("study inventory differs or changed during read")
        return manifest, plan, report
    except TiveloError as exc:
        if exc.code == ErrorCode.ARTIFACT_CORRUPT:
            raise
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Study report failed semantic verification"
        ) from exc
    except (ValueError, OSError, TypeError, KeyError) as exc:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Study report failed semantic verification"
        ) from exc
