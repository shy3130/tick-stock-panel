"""Independent data CLI. Inputs and data root are always explicit."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError
from tivelo_contracts.base import Contract, Key, UTCDateTime, canonical_bytes
from tivelo_contracts.data import BarQuery, Coverage, EventQuery, HistoryQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.extension_fields import ExtensionQuery

from .engine import DataEngine
from .jsonl import JSONLinesProvider
from .providers import (
    FixedSourceResolver,
    SyncContext,
    SyncPlan,
    SyncRequest,
    close_source,
    local_context,
)
from .storage import Candidate


class SymbolLookup(Contract):
    symbol: Key
    source_id: Key
    venue_id: Key
    effective_at: UTCDateTime
    as_of: UTCDateTime


def _environment_context():
    base = local_context()
    return SyncContext(
        lambda reference: {"token": os.environ.get(reference, "")}, base.cancelled, base.progress
    )


def _http_source(args):
    from .adapters.cn_daily import CNFactorRequest, CNRawDailyRequest, CNRawMinuteRequest

    try:
        import yaml

        from .http.config import load_config

        if args.command.endswith("-factors"):
            from .http.cn_factors import CNHTTPFactorProvider as Provider

            request_type = CNFactorRequest
        elif args.command.endswith("-minute"):
            from .http.cn_minute import CNHTTPMinuteProvider as Provider

            request_type = CNRawMinuteRequest
        else:
            from .http.cn_daily import CNDailyProvider as Provider

            request_type = CNRawDailyRequest
    except ModuleNotFoundError as exc:
        if exc.name not in {"httpx", "tivelo_market_cn", "yaml"}:
            raise
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY, "Install tivelo-data[cn-http] for HTTP sources"
        ) from None

    capture = request_type.model_validate_json(args.capture.read_bytes())
    try:
        source = Provider(capture, load_config(args.source))
    except (yaml.YAMLError, AttributeError, TypeError, ValueError, OverflowError, RecursionError):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid HTTP source declaration") from None
    return capture.request, source, _environment_context()


def _stock_source(args):
    from .adapters.cn_daily import CNFactorRequest, CNRawDailyRequest

    profile = args.command.partition("-")[2]
    vendor = profile.partition("-")[0]
    factors = profile.endswith("-factors")
    try:
        if profile == "tickflow-factors":
            from .tickflow_connection import TickFlowDailyConfig as Config
            from .tickflow_factors import TickFlowFactorProvider as Provider
        elif vendor == "tickflow":
            from .tickflow_daily import TickFlowDailyConfig as Config
            from .tickflow_daily import TickFlowDailyProvider as Provider
        elif vendor == "fuyao":
            from .fuyao_daily import FuyaoDailyConfig as Config

            if factors:
                from .fuyao_factors import FuyaoFactorProvider as Provider
            else:
                from .fuyao_daily import FuyaoDailyProvider as Provider
        else:
            from .tdx_daily import TdxQuantDailyConfig as Config

            if factors:
                from .tdx_factors import TdxQuantFactorProvider as Provider
            else:
                from .tdx_daily import TdxQuantDailyProvider as Provider
    except ModuleNotFoundError as exc:
        if exc.name not in {"httpx", "tickflow", "tdxaidata", "tivelo_market_cn"}:
            raise
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            f"Install tivelo-data[cn-{vendor}] for this {vendor} source",
        ) from None

    capture_type = CNFactorRequest if factors else CNRawDailyRequest
    capture = capture_type.model_validate_json(args.capture.read_bytes())
    config = Config.model_validate_json(args.source.read_bytes())
    return capture.request, Provider(capture, config), _environment_context()


def _repository_source(args):
    from .adapters.cn_daily import (
        RepositoryDailyRequest,
        RepositoryFactorRequest,
        RepositoryMinuteRequest,
    )
    from .legacy.repository_files import DAILY_TABLES, FACTOR_TABLE, MINUTE_TABLE

    adapter = TypeAdapter(
        Annotated[
            RepositoryDailyRequest | RepositoryFactorRequest | RepositoryMinuteRequest,
            Field(discriminator="layout"),
        ]
    )
    capture = adapter.validate_json(args.capture.read_bytes())
    root = args.repository.resolve(strict=True)
    destination = args.root.resolve()
    if destination == root or any(
        destination.is_relative_to(root / table)
        for table in (*DAILY_TABLES.values(), FACTOR_TABLE, MINUTE_TABLE)
    ):
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Destination overlaps legacy data partitions")
    if isinstance(capture, RepositoryFactorRequest):
        from .repository_factors import RepositoryFactorProvider

        return capture.request, RepositoryFactorProvider(root, capture), local_context()

    try:
        if isinstance(capture, RepositoryMinuteRequest):
            from .repository_minute import RepositoryMinuteProvider

            return capture.request, RepositoryMinuteProvider(root, capture), local_context()
        from .repository_daily import RepositoryDailyProvider
    except ModuleNotFoundError as exc:
        if exc.name != "tivelo_market_cn":
            raise
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Install tivelo-data[cn-repository] for repository bar imports",
        ) from None

    return capture.request, RepositoryDailyProvider(root, capture), local_context()


def _native_source(args):
    from .native_source import NativeDataInput, NativeDataProvider, installation_credentials

    capture = NativeDataInput.model_validate_json(args.capture.read_bytes())
    locked = args.saved_plan
    if locked is not None and locked.extension is None:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Saved plan does not identify a signed source")
    provider = NativeDataProvider(
        args.extension_state, capture, pin=locked.extension if locked else None
    )
    base = local_context()
    return (
        capture.request,
        provider,
        SyncContext(installation_credentials, base.cancelled, base.progress),
    )


def _jsonl_source(args):
    request = SyncRequest.model_validate_json(args.request.read_bytes())
    source = JSONLinesProvider(
        args.input,
        request,
        source_id=args.source_id,
        knowledge_basis=args.knowledge_basis,
        bar_convention_id=args.bar_convention,
        coverage=Coverage.model_validate_json(args.coverage.read_bytes())
        if args.coverage
        else None,
    )
    return request, source, local_context()


def main(argv=None, *, map_paths=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser(
        "audit-repository-daily", help="Check numeric precision in physical daily partitions"
    )
    audit.add_argument("--repository", type=Path, required=True)
    audit.add_argument(
        "--request", type=Path, required=True, help="RepositoryPrecisionRequest JSON"
    )
    observe = sub.add_parser(
        "observe-tsp-field", help="Witness a field and export its immutable research input"
    )
    observe.add_argument("--repository", type=Path, required=True, help="TSP data directory")
    observe.add_argument("--mapping", type=Path, required=True)
    observe.add_argument(
        "--output", type=Path, required=True, help="Directory for content-addressed JSONL files"
    )
    for name, operation in (
        ("plan", "plan"),
        ("sync", "sync"),
        ("import-jsonl", "import"),
        ("plan-repository", "plan"),
        ("sync-repository", "sync"),
        ("import-repository", "import"),
        ("plan-http", "plan"),
        ("sync-http", "sync"),
        ("import-http", "import"),
        *((f"{op}-http-factors", op) for op in ("plan", "sync", "import")),
        *((f"{op}-http-minute", op) for op in ("plan", "sync", "import")),
        ("plan-provider", "plan"),
        ("sync-provider", "sync"),
        ("import-provider", "import"),
        *(
            (f"{op}-{vendor}", op)
            for vendor in (
                "tickflow",
                "tickflow-factors",
                "fuyao",
                "fuyao-factors",
                "tdxquant",
                "tdxquant-factors",
            )
            for op in ("plan", "sync", "import")
        ),
    ):
        command = sub.add_parser(name)
        if operation != "plan":
            command.add_argument("--locked-plan", type=Path, help="Require this exact saved plan")
        if name.endswith("-provider"):
            command.set_defaults(open_source=_native_source, operation=operation)
            command.add_argument("--capture", type=Path, required=True, help="NativeDataInput JSON")
            command.add_argument(
                "--extension-state", type=Path, required=True, help="Signed extension installation"
            )
            continue
        if name.endswith("-repository"):
            command.set_defaults(open_source=_repository_source, operation=operation)
            command.add_argument(
                "--capture",
                type=Path,
                required=True,
                help="Repository daily, minute or factor request JSON",
            )
            command.add_argument(
                "--repository", type=Path, required=True, help="Source v1 data directory"
            )
            continue
        is_http = name.endswith(("-http", "-http-factors", "-http-minute"))
        if is_http or name.endswith(("-tickflow", "-fuyao", "-tdxquant", "-factors")):
            command.set_defaults(
                open_source=_http_source if is_http else _stock_source, operation=operation
            )
            command.add_argument(
                "--capture",
                type=Path,
                required=True,
                help="Market request and ordered instrument bindings in JSON",
            )
            command.add_argument(
                "--source",
                type=Path,
                required=True,
                help="Declared HTTP source YAML or JSON"
                if is_http
                else "Vendor connection configuration JSON",
            )
            continue
        command.set_defaults(open_source=_jsonl_source, operation=operation)
        command.add_argument("--request", type=Path, required=True)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--source-id", required=True)
        command.add_argument(
            "--knowledge-basis",
            required=True,
            choices=("historical_versions", "latest_reconstruction", "unknown"),
        )
        command.add_argument("--bar-convention")
        command.add_argument(
            "--coverage",
            type=Path,
            help="Explicit source coverage report; never inferred from an empty file",
        )
    for name in ("validate", "publish"):
        sub.add_parser(name).add_argument("--candidate", type=Path, required=True)
    publications = sub.add_parser(
        "publications", help="Read committed data versions from a saved cursor"
    )
    publications.add_argument("--dataset", help="Watch a single dataset ID")
    publications.add_argument("--limit", type=int, default=100)
    since = publications.add_mutually_exclusive_group()
    since.add_argument("--after", type=Path, help="File containing a previous response's cursor")
    since.add_argument(
        "--tail",
        action="store_true",
        help="Start at the current end without reading existing events",
    )
    sub.add_parser("snapshot").add_argument("--version", action="append", required=True)
    sub.add_parser("inspect").add_argument("--snapshot", required=True)
    export = sub.add_parser("export")
    export.add_argument("--snapshot", required=True)
    export.add_argument("--output", type=Path, required=True)
    symbol_command = sub.add_parser(
        "resolve-symbol", help="Look up a source and venue code in a fixed snapshot"
    )
    symbol_command.add_argument("--snapshot", required=True)
    symbol_command.add_argument("--request", type=Path, required=True, help="SymbolLookup JSON")
    query = sub.add_parser("query")
    query.add_argument("--snapshot", required=True)
    query.add_argument("--request", type=Path, required=True)
    queries = {
        "extension_value": (ExtensionQuery, "extension_values"),
        "bar": (BarQuery, "bars"),
        "opening_observation": (EventQuery, "openings"),
        "adjustment_factor": (HistoryQuery, "factors"),
        "instrument": (HistoryQuery, "instruments"),
        "corporate_action": (HistoryQuery, "actions"),
    }
    try:
        from tivelo_contracts.order_books import OrderBookQuery
    except ModuleNotFoundError as exc:
        if exc.name != "tivelo_contracts.order_books":
            raise
    else:
        queries["order_book_observation"] = (OrderBookQuery, "order_books")
    try:
        from tivelo_contracts.trade_prints import TradePrintQuery
    except ModuleNotFoundError as exc:
        if exc.name != "tivelo_contracts.trade_prints":
            raise
    else:
        queries["trade_print"] = (TradePrintQuery, "trade_prints")
    query.add_argument("--record-type", choices=tuple(queries), default="bar")
    args = parser.parse_args(argv)
    try:
        if map_paths is not None:
            map_paths(args)
        if args.command == "audit-repository-daily":
            from .repository_precision import RepositoryPrecisionRequest, audit_repository_precision

            request = RepositoryPrecisionRequest.model_validate_json(args.request.read_bytes())
            report = audit_repository_precision(args.repository, request)
            print(canonical_bytes(report).decode())
            return 0 if report.status == "clean" else 1
        engine = DataEngine(args.root)
        if args.command == "observe-tsp-field":
            from .extension_capture import TSPFieldCapture, observe_tsp_field

            mapping = TSPFieldCapture.model_validate_json(args.mapping.read_bytes())
            result = observe_tsp_field(args.root, args.repository, mapping, args.output)
            print(json.dumps(result, ensure_ascii=False))
        elif hasattr(args, "open_source"):
            locked_path = getattr(args, "locked_plan", None)
            args.saved_plan = (
                SyncPlan.model_validate_json(locked_path.read_bytes()) if locked_path else None
            )
            request, source, context = args.open_source(args)
            try:
                engine.resolver = FixedSourceResolver(source, source.configuration_digest)
                plan = engine.plan_sync(request)
                if args.saved_plan is not None and args.saved_plan != plan:
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Source or scope differs from the saved plan"
                    )
            except BaseException as exc:
                close_source(source, primary=exc)
                raise
            if args.operation == "plan":
                close_source(source)
                print(canonical_bytes(plan).decode())
            else:
                candidate = engine.sync(plan, context)
                if args.operation == "sync":
                    print(canonical_bytes(candidate).decode())
                else:
                    version = engine.publish(candidate)
                    print(canonical_bytes(engine.create_snapshot((version.version_id,))).decode())
        elif args.command in ("validate", "publish"):
            candidate = Candidate.model_validate_json(args.candidate.read_bytes())
            value = (
                engine.validate(candidate)
                if args.command == "validate"
                else engine.publish(candidate)
            )
            print(canonical_bytes(value).decode())
        elif args.command == "publications":
            from .publications import PublicationCursor

            cursor = (
                PublicationCursor.model_validate_json(args.after.read_bytes())
                if args.after
                else None
            )
            page = engine.publications(
                after=cursor, dataset_id=args.dataset, limit=args.limit, tail=args.tail
            )
            print(canonical_bytes(page).decode())
        elif args.command == "snapshot":
            print(canonical_bytes(engine.create_snapshot(tuple(args.version))).decode())
        elif args.command == "export":
            print(canonical_bytes(engine.export_snapshot(args.snapshot, args.output)).decode())
        elif args.command == "inspect":
            with engine.open_reader(args.snapshot) as reader:
                print(
                    json.dumps(
                        {
                            "snapshot": reader.manifest.model_dump(mode="json"),
                            "datasets": [
                                value.model_dump(mode="json") for value in reader.datasets
                            ],
                        }
                    )
                )
        elif args.command == "resolve-symbol":
            lookup = SymbolLookup.model_validate_json(args.request.read_bytes())
            with engine.open_reader(args.snapshot) as reader:
                instrument_id = reader.resolve_symbol(**lookup.model_dump())
            print(json.dumps({"instrument_id": instrument_id}))
        elif args.command == "query":
            kind, method = queries[args.record_type]
            request = kind.model_validate_json(args.request.read_bytes())
            with engine.open_reader(args.snapshot) as reader:
                for record in getattr(reader, method)(request):
                    print(canonical_bytes(record).decode())
        return 0
    except TiveloError as exc:
        from .repository_diagnostics import RepositorySourceError

        error = {"code": exc.code.value, "message": str(exc), "retryable": exc.retryable}
        if isinstance(exc, RepositorySourceError):
            error["source_issues"] = [issue.model_dump(mode="json") for issue in exc.issues]
        if exc.retry_after is not None:
            error["retry_after"] = exc.retry_after
        print(json.dumps(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print(
            json.dumps(
                {
                    "code": "PUBLICATION_FAILED",
                    "message": "Storage or source I/O failed",
                    "retryable": False,
                }
            ),
            file=sys.stderr,
        )
        return 1
    except (ValidationError, ValueError) as exc:
        # Do not serialize supplier payloads or credentials from validation errors.
        print(
            json.dumps(
                {
                    "code": "CONTRACT_INVALID",
                    "message": "Input or storage operation failed",
                    "error_type": type(exc).__name__,
                    "retryable": False,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
