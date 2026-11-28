"""Independent Tivelo backtests: seal, prepare, run and inspect immutable archives."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import DefinitionReference

from .archives import MAX_MANIFEST_BYTES, inspect_archive, read_result
from .definitions import seal_definition
from .extension_cli import add_commands, local_runtime, manage
from .runner import RunRequest, execute_run, prepare_run, replay_result, restore_run


def _input(path):
    with path.open("rb") as source:
        payload = source.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Input exceeds its byte budget")
    return payload


def _seal(args):
    root = args.root.expanduser().absolute()
    references = tuple(
        DefinitionReference.model_validate_json(_input(path)) for path in args.dependency
    )
    bundle = seal_definition(
        root,
        definition_id=args.id,
        version=args.version,
        kind=args.kind,
        entrypoint=args.entrypoint,
        files=tuple(args.file),
        dependencies=references,
        requirements=tuple(args.requirement),
        language=args.language,
    )
    encoded = canonical_bytes(bundle)
    target = root / "definition.json"
    with tempfile.TemporaryDirectory(prefix=".tivelo-seal-", dir=root) as temporary:
        staged = Path(temporary) / "definition.json"
        with staged.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(staged, target)
        except FileExistsError:
            if target.is_symlink() or _input(target) != encoded:
                raise TiveloError(
                    ErrorCode.DEFINITION_CONFLICT,
                    "Definition manifest already exists with different content",
                ) from None
    return bundle


def main(argv=None, *, map_paths=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seal = sub.add_parser("seal", help="Hash explicit definition files; execute nothing")
    seal.add_argument("--root", type=Path, required=True)
    seal.add_argument("--id", required=True)
    seal.add_argument("--version", required=True)
    seal.add_argument(
        "--kind", choices=("strategy", "factor", "signal", "indicator"), default="strategy"
    )
    seal.add_argument("--entrypoint", required=True)
    seal.add_argument("--language", choices=("trusted_python", "controlled_daily_v1"), default="trusted_python")
    seal.add_argument("--file", action="append", required=True)
    seal.add_argument("--dependency", type=Path, action="append", default=[])
    seal.add_argument("--requirement", action="append", default=[])
    for name, label in (
        ("seal-legacy-matrix", "daily matrix"),
        ("seal-legacy-polars", "daily Polars"),
        ("seal-legacy-composite", "daily composite"),
        ("seal-legacy-minute", "minute filter"),
    ):
        legacy_seal = sub.add_parser(name, help=f"Seal an existing {label} strategy")
        legacy_seal.add_argument("--source", type=Path, required=True)
        legacy_seal.add_argument("--output", type=Path, required=True)
        legacy_seal.add_argument("--version", required=True)
        legacy_seal.add_argument("--requirement", action="append", default=[])
        if name == "seal-legacy-composite":
            legacy_seal.add_argument("--dependency", type=Path, action="append", required=True)
    prepare = sub.add_parser("prepare", help="Validate inputs and archive a real RunLock")
    prepare.add_argument("--request", type=Path, required=True)
    prepare.add_argument("--definition", type=Path, required=True)
    prepare.add_argument("--dependencies", type=Path)
    prepare.add_argument("--snapshot", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run", help="Execute the archived strategy with its locked semantics")
    run.add_argument("--run", type=Path, required=True)
    run.add_argument("--snapshot", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    inspect = sub.add_parser("inspect", help="Verify and read archives without running strategies")
    inspect.add_argument("--archive", type=Path, required=True)
    inspect.add_argument("--kind", choices=("run", "result", "study", "legacy-robust"), default="result")
    sub.add_parser("request-schema", help="Print the JSON Schema for prepare requests")
    sub.add_parser("controlled-schema", help="Print the bounded daily strategy JSON Schema")
    sub.add_parser("study-schema", help="Print the grid-search and walk-forward JSON Schema")
    study = sub.add_parser("study", help="Run a locked grid search or walk-forward study")
    study.add_argument("--request", type=Path, required=True)
    study.add_argument("--run", type=Path, required=True)
    study.add_argument("--snapshot", type=Path, required=True)
    study.add_argument("--output", type=Path, required=True)
    legacy_robust = sub.add_parser("legacy-robust", help="Optimize a sealed legacy daily or minute strategy")
    legacy_robust.add_argument("--request", type=Path, required=True)
    legacy_robust.add_argument("--definition", type=Path, required=True)
    legacy_robust.add_argument("--dependencies", type=Path)
    legacy_robust.add_argument("--snapshot", type=Path, required=True)
    legacy_robust.add_argument("--output", type=Path, required=True)
    sub.add_parser("legacy-robust-schema", help="Print the legacy optimization JSON Schema")
    legacy_robust_replay = sub.add_parser("legacy-robust-replay", help="Reproduce an archived legacy optimization")
    result_replay = sub.add_parser("result-replay", help="Reproduce a single locked result")
    study_replay = sub.add_parser(
        "study-replay", help="Reproduce a study using its archived request and definitions"
    )
    restore = sub.add_parser("restore-run", help="Recover a prepared run from a single result")
    for command in (result_replay, study_replay, legacy_robust_replay, restore):
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    for command in (result_replay, study_replay, legacy_robust_replay):
        command.add_argument("--snapshot", type=Path, required=True)
    for command in (prepare, run, study, result_replay, study_replay):
        command.add_argument("--extension-state", type=Path, help="Use this explicitly managed local extension installation")
    add_commands(sub)
    args = parser.parse_args(argv)
    try:
        if map_paths is not None:
            map_paths(args)
        if args.command == "extensions":
            print(json.dumps(manage(args), sort_keys=True, ensure_ascii=False))
            return 0
        if args.command in ("request-schema", "controlled-schema", "study-schema"):
            from .controlled import ControlledProgram
            from .study_models import StudyRequest

            model = {"request-schema": RunRequest, "controlled-schema": ControlledProgram, "study-schema": StudyRequest}[args.command]
            print(json.dumps(model.model_json_schema(), sort_keys=True))
            return 0
        if args.command == "legacy-robust-schema":
            from .legacy.optimization_archive import LegacyRobustRequest

            print(json.dumps(LegacyRobustRequest.model_json_schema(), sort_keys=True))
            return 0
        if args.command == "seal":
            output = _seal(args)
        elif args.command in {"seal-legacy-matrix", "seal-legacy-polars", "seal-legacy-composite", "seal-legacy-minute"}:
            from .legacy.matrix_source import (
                seal_legacy_composite,
                seal_legacy_matrix,
                seal_legacy_minute,
                seal_legacy_polars,
            )

            sealer = {
                "seal-legacy-matrix": seal_legacy_matrix,
                "seal-legacy-polars": seal_legacy_polars,
                "seal-legacy-composite": seal_legacy_composite,
                "seal-legacy-minute": seal_legacy_minute,
            }[args.command]
            options = {}
            if args.command == "seal-legacy-composite":
                options["dependencies"] = tuple(
                    DefinitionReference.model_validate_json(_input(path)) for path in args.dependency
                )
            output = sealer(
                _input(args.source), args.output,
                version=args.version, requirements=tuple(args.requirement), **options,
            )
        elif args.command == "prepare":
            with redirect_stdout(sys.stderr):
                output = prepare_run(
                    RunRequest.model_validate_json(_input(args.request)),
                    snapshot=args.snapshot,
                    definition=args.definition,
                    dependency_root=args.dependencies,
                    destination=args.output,
                    extension_runtime=local_runtime(args),
                )
        elif args.command == "run":
            with redirect_stdout(sys.stderr):
                output = execute_run(run=args.run, snapshot=args.snapshot, destination=args.output,
                                     extension_runtime=local_runtime(args))
        elif args.command == "restore-run":
            output = restore_run(archive=args.archive, destination=args.output)
        elif args.command == "result-replay":
            with redirect_stdout(sys.stderr):
                output = replay_result(
                    archive=args.archive, snapshot=args.snapshot, destination=args.output,
                    extension_runtime=local_runtime(args),
                )
        elif args.command == "study":
            from .studies import run_study
            from .study_models import StudyRequest

            with redirect_stdout(sys.stderr):
                output = run_study(StudyRequest.model_validate_json(_input(args.request)),
                                   run=args.run, snapshot=args.snapshot, destination=args.output,
                                   extension_runtime=local_runtime(args))
        elif args.command == "study-replay":
            from .studies import replay_study

            with redirect_stdout(sys.stderr):
                output = replay_study(archive=args.archive, snapshot=args.snapshot, destination=args.output,
                                      extension_runtime=local_runtime(args))
        elif args.command == "legacy-robust":
            from .legacy.optimization_archive import LegacyRobustRequest, run_legacy_robust

            with redirect_stdout(sys.stderr):
                output = run_legacy_robust(
                    LegacyRobustRequest.model_validate_json(_input(args.request)),
                    definition=args.definition,
                    dependency_root=args.dependencies,
                    snapshot=args.snapshot,
                    destination=args.output,
                )
        elif args.command == "legacy-robust-replay":
            from .legacy.optimization_archive import replay_legacy_robust

            with redirect_stdout(sys.stderr):
                output = replay_legacy_robust(
                    archive=args.archive, snapshot=args.snapshot, destination=args.output
                )
        elif args.kind == "legacy-robust":
            from .legacy.optimization_archive import read_legacy_robust

            manifest, lock, report = read_legacy_robust(args.archive)
            print(json.dumps({"archive_id": manifest.archive_id, "lock": lock.model_dump(mode="json"),
                              "report": report}, sort_keys=True))
            return 0
        elif args.kind == "study":
            from .study_archives import read_study

            manifest, plan, report = read_study(args.archive)
            print(json.dumps({"archive_id": manifest.archive_id, "plan": plan.model_dump(mode="json"),
                              "report": report.model_dump(mode="json")}, sort_keys=True))
            return 0
        elif args.kind == "run":
            output, _ = inspect_archive(args.archive)
        else:
            lock, result, metrics = read_result(args.archive)
            print(
                json.dumps(
                    {
                        "lock_id": lock.lock_id,
                        "snapshot_id": lock.contents.spec.snapshot_id,
                        "price_view": lock.contents.spec.price_view.model_dump(mode="json"),
                        "scale_proof": lock.contents.scale_proof.model_dump(mode="json") if lock.contents.scale_proof else None,
                        "accounting_model": result.accounting_model,
                        "semantics_version": result.semantics_version,
                        "market_profile": lock.contents.spec.market_profile.model_dump(mode="json"),
                        "knowledge_requirement": lock.contents.spec.knowledge_requirement,
                        "metrics": metrics.model_dump(mode="json"),
                        "diagnostics": result.diagnostics,
                        "diagnostic_event_count": len(result.diagnostic_events),
                    },
                    sort_keys=True,
                )
            )
            return 0
        print(canonical_bytes(output).decode("utf-8"))
        return 0
    except KeyboardInterrupt:
        error = TiveloError(
            ErrorCode.CANCELLED, "Execution interrupted; inspect the destination before retrying"
        )
        status = 130
    except TiveloError as exc:
        error, status = exc, 1
    except OSError:
        error = TiveloError(ErrorCode.PUBLICATION_FAILED, "Input or archive I/O failed")
        status = 1
    except (ValueError, TypeError):
        error = TiveloError(ErrorCode.CONTRACT_INVALID, "Input failed contract validation")
        status = 1
    print(
        json.dumps({"code": error.code, "message": str(error), "retryable": error.retryable}),
        file=sys.stderr,
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
