"""Immutable run inputs and columnar results; manifest.json commits publication.

The archive owns definition copies, not market data or installed environments.
Snapshot and runtime identities stay in the RunLock. Reading an old result does
not load strategy/market code or require an active commercial entitlement.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from typing import Literal

import polars as pl
from pydantic import Field
from tivelo_contracts.artifacts import ArchiveContents, ArchiveFile, ResultArchive, RunArchive
from tivelo_contracts.base import (
    Contract,
    Currency,
    Digest,
    ExactDecimal,
    Key,
    NonNegativeDecimal,
    PositiveDecimal,
    canonical_bytes,
)
from tivelo_contracts.definitions import DefinitionBundle
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import (
    BacktestResult,
    EquityPoint,
    ExecutionDiagnostic,
    LedgerEntry,
    OrderEvent,
    Portfolio,
    RunLock,
)

from .definitions import CheckedDefinitions, DefinitionLoader

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_TABLE_ROWS = 5_000_000
_DECIMAL, _TIME = pl.Decimal(38, 12), pl.Datetime("us", "UTC")
_FEE = pl.Struct(
    {
        "fee_id": pl.String,
        "amount": _DECIMAL,
        "rate": _DECIMAL,
        "minimum_applied": pl.Boolean,
        "rule_revision": pl.String,
        "source_reference": pl.String,
    }
)
_TABLES = {
    "orders.parquet": (
        "orders",
        OrderEvent,
        {
            "order_id": pl.String,
            "instrument_id": pl.String,
            "side": pl.String,
            "status": pl.String,
            "decision_at": _TIME,
            "submitted_at": _TIME,
            "eligible_at": _TIME,
            "filled_at": _TIME,
            "event_at": _TIME,
            "requested_quantity": _DECIMAL,
            "filled_quantity": _DECIMAL,
            "raw_fill_price": _DECIMAL,
            "fee": _DECIMAL,
            "fee_components": pl.List(_FEE),
            "currency": pl.String,
            "reason": pl.String,
        },
    ),
    "equity.parquet": (
        "equity",
        EquityPoint,
        {
            "at": _TIME,
            "equity": _DECIMAL,
            "currency": pl.String,
        },
    ),
    "ledger.parquet": (
        "ledger",
        LedgerEntry,
        {
            "entry_id": pl.String,
            "at": _TIME,
            "kind": pl.String,
            "reference_id": pl.String,
            "instrument_id": pl.String,
            "currency": pl.String,
            "cash_delta": _DECIMAL,
            "quantity_delta": _DECIMAL,
            "receivable_delta": _DECIMAL,
        },
    ),
    "diagnostic-events.parquet": (
        "diagnostic_events",
        ExecutionDiagnostic,
        {
            "code": pl.String,
            "at": _TIME,
            "instrument_id": pl.String,
        },
    ),
}


def _result_tables(encoding):
    if encoding == "parquet-v1":
        return _TABLES
    if encoding != "parquet-v2":
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Unsupported result table encoding")
    attribute, model, fields = _TABLES["orders.parquet"]
    return {
        **_TABLES,
        "orders.parquet": (
            attribute,
            model,
            {**fields, "order_type": pl.String, "raw_limit_price": _DECIMAL},
        ),
    }


class ResultSummary(Contract):
    result_version: Literal["2.0"] = "2.0"
    lock_id: Digest
    semantics_version: Key
    accounting_model: Key
    final_portfolio: Portfolio
    diagnostics: tuple[Key, ...] = ()


class RunMetrics(Contract):
    metrics_version: Literal["1.0"] = "1.0"
    calculation: Literal["daily_equity_decimal12_half_even", "checkpoint_equity_decimal12_half_even"] = (
        "daily_equity_decimal12_half_even"
    )
    currency: Currency
    initial_capital: PositiveDecimal
    final_equity: ExactDecimal
    total_return: ExactDecimal
    maximum_drawdown: NonNegativeDecimal
    filled_orders: int = Field(strict=True, ge=0)


def calculate_metrics(lock: RunLock, result: BacktestResult) -> RunMetrics:
    spec = lock.contents.spec
    continuous = spec.semantics_version == "strict.continuous.v1"
    if (
        result.lock_id != lock.lock_id
        or result.semantics_version != spec.semantics_version
        or result.accounting_model != spec.accounting_model
        or not result.equity
        or any(point.currency != spec.base_currency or point.equity < 0 for point in result.equity)
        or any(a.at >= b.at for a, b in zip(result.equity, result.equity[1:], strict=False))
        or any(
            value.currency != spec.base_currency
            for value in (
                *result.final_portfolio.cash,
                *result.final_portfolio.positions,
                *result.orders,
                *result.ledger,
            )
        )
    ):
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Result does not match its locked execution")
    with localcontext(Context(prec=96)):
        peak, drawdown = spec.initial_capital, Decimal(0)
        for point in result.equity:
            peak = max(peak, point.equity)
            drawdown = max(drawdown, (peak - point.equity) / peak)
        quantum = Decimal("0.000000000001")
        return RunMetrics(
            calculation=("checkpoint_equity_decimal12_half_even"
                         if continuous
                         else "daily_equity_decimal12_half_even"),
            currency=spec.base_currency,
            initial_capital=spec.initial_capital,
            final_equity=result.equity[-1].equity,
            total_return=(result.equity[-1].equity / spec.initial_capital - 1).quantize(
                quantum, rounding=ROUND_HALF_EVEN
            ),
            maximum_drawdown=drawdown.quantize(quantum, rounding=ROUND_HALF_EVEN),
            filled_orders=(len({order.order_id for order in result.orders if order.filled_quantity})
                           if continuous else sum(order.status == "filled" for order in result.orders)),
        )


def _sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _durable_mkdir(path):
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        _sync_directory(directory.parent)


def _safe_path(root, relative):
    if root.is_symlink() or not root.is_dir():
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Archive root is unavailable or a symbolic link"
        )
    path = root
    for part in Path(relative).parts:
        path /= part
        if path.is_symlink():
            raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive contains a symbolic link")
    if not stat.S_ISREG(path.stat().st_mode):
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive contains a nonregular file")
    return path


def _read(root, relative, limit=MAX_MANIFEST_BYTES):
    path = _safe_path(root, relative)
    if path.stat().st_size > limit:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive file exceeds its byte budget")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive file exceeds its byte budget")
    return content


def _file_state(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _record(root, relative, rows=None, *, expected_size=None):
    path = _safe_path(root, relative)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_ARCHIVE_BYTES or (
            expected_size is not None and before.st_size != expected_size
        ):
            raise TiveloError(
                ErrorCode.ARTIFACT_CORRUPT, "Archive file size differs or exceeds budget"
            )
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        # Windows can report different ctimes for pathname and descriptor stats.
        with path.open("rb") as current:
            path_state = os.fstat(current.fileno())
    if not (_file_state(before) == _file_state(after) == _file_state(path_state)):
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive file changed during verification")
    return ArchiveFile(path=relative, sha256=digest, size_bytes=after.st_size, row_count=rows)


def _definition_inventory(root, strategy, dependencies, inventory):
    """Check declared closures and source digests, without parsing/executing Python."""
    references = (strategy, *dependencies)
    if len(references) > 64:
        raise ValueError("definition graph exceeds its node budget")
    by_id = {reference.definition_id: reference for reference in references}
    bundles, expected = {}, set()
    count, size = 0, 0
    for reference in references:
        prefix = "definitions/" + reference.sha256
        marker = prefix + "/definition.json"
        bundle = DefinitionBundle.model_validate_json(_read(root, marker, 1_048_576))
        if bundle.reference(reference.parameters_json) != reference:
            raise ValueError("definition differs from its reference")
        bundles[reference.definition_id] = bundle
        expected.add(marker)
        for item in bundle.contents.files:
            path = prefix + "/" + item.path
            if path not in inventory or (inventory[path].sha256, inventory[path].size_bytes) != (
                item.sha256,
                item.size_bytes,
            ):
                raise ValueError("source declaration differs from archive inventory")
            count += 1
            size += item.size_bytes
            expected.add(path)
    if count > 4096 or size > 64 * 1024 * 1024:
        raise ValueError("definition graph exceeds its file budget")
    visited, visiting = set(), set()

    def visit(identity, depth):
        if identity in visiting or depth > 16:
            raise ValueError("cyclic or excessive definition graph")
        if identity in visited:
            return
        visiting.add(identity)
        for dependency in bundles[identity].contents.dependencies:
            if by_id.get(dependency.definition_id) != dependency:
                raise ValueError("incomplete or inconsistent definition graph")
            visit(dependency.definition_id, depth + 1)
        visiting.remove(identity)
        visited.add(identity)

    visit(strategy.definition_id, 1)
    if visited != set(by_id):
        raise ValueError("undeclared graph components")
    return bundles[strategy.definition_id], expected


def inspect_archive(directory, *, result=False):
    """Verify all declared files without importing archived strategy or market code."""
    root = Path(directory).expanduser().absolute()
    model = ResultArchive if result else RunArchive
    try:
        manifest = model.model_validate_json(_read(root, "manifest.json"))
        if sum(file.size_bytes for file in manifest.contents.files) > MAX_ARCHIVE_BYTES:
            raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive exceeds its byte budget")
        for file in manifest.contents.files:
            if _record(root, file.path, file.row_count, expected_size=file.size_bytes) != file:
                raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive file digest differs")
        lock = RunLock.model_validate_json(_read(root, "run-lock.json"))
        if lock.lock_id != manifest.contents.lock_id:
            raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive refers to a different RunLock")
        return manifest, lock
    except (OSError, ValueError, TypeError) as exc:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Archive failed content verification"
        ) from exc


def _write(stage, relative, payload):
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _write_inputs(stage, lock, definitions):
    DefinitionLoader().validate(definitions)
    if (
        definitions.root != lock.contents.spec.strategy
        or definitions.dependencies != lock.contents.spec.dependencies
    ):
        raise TiveloError(ErrorCode.DEFINITION_CONFLICT, "Definitions differ from the RunLock")
    _write(stage, "run-lock.json", canonical_bytes(lock))
    return ["run-lock.json", *_write_definitions(stage, definitions)]


def _write_definitions(stage, definitions):
    files = []
    for node in definitions.nodes:
        prefix = f"definitions/{node.bundle.bundle_id}"
        for relative, payload in (("definition.json", canonical_bytes(node.bundle)), *node.files):
            path = f"{prefix}/{relative}"
            _write(stage, path, payload)
            files.append(path)
    return files


def _cancel(observer):
    if observer is not None and observer.cancelled():
        raise TiveloError(ErrorCode.CANCELLED, "Archive publication cancelled before completion")


def _publish(stage, target, manifest, observer, *, inspect_existing=None):
    encoded = canonical_bytes(manifest)
    if (
        len(encoded) > MAX_MANIFEST_BYTES
        or sum(file.size_bytes for file in manifest.contents.files) > MAX_ARCHIVE_BYTES
    ):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Archive exceeds its publication budget")

    def existing():
        previous = (
            inspect_existing(target)
            if inspect_existing is not None
            else inspect_archive(target, result=isinstance(manifest, ResultArchive))[0]
        )
        if previous != manifest:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Destination contains a different archive")
        return previous

    if target.exists() or target.is_symlink():
        return existing()
    _write(stage, "manifest.json", encoded)
    files, directories, published = [], [], False
    try:
        try:
            target.mkdir()
        except FileExistsError:
            return existing()
        directories.append(target)
        for file in manifest.contents.files:
            _cancel(observer)
            current = target
            for part in Path(file.path).parts[:-1]:
                current /= part
                if current not in directories:
                    current.mkdir()
                    directories.append(current)
            destination = target / file.path
            os.link(stage / file.path, destination)
            files.append(destination)
        for directory in reversed(directories):
            _sync_directory(directory)
        _sync_directory(target.parent)
        _cancel(observer)
        try:
            os.link(stage / "manifest.json", target / "manifest.json")
        finally:
            # Also protect the committed archive if an interrupt arrives just after link().
            try:
                published = os.path.samefile(stage / "manifest.json", target / "manifest.json")
            except OSError:
                pass
        _sync_directory(target)
        return manifest
    except KeyboardInterrupt:
        if not published:
            raise
        # Publication is the terminal boundary: a later cancellation cannot undo success.
        _sync_directory(target)
        return manifest
    finally:
        if not published:
            for path in reversed(files):
                try:
                    path.unlink()
                except OSError:
                    pass
            for path in reversed(directories):
                try:
                    path.rmdir()
                except OSError:
                    pass


def _publish_archive(
    lock, definitions, destination, result=None, observer=None, *, expected_archive=None
):
    lock = RunLock.model_validate_json(canonical_bytes(lock))
    requested = Path(destination).expanduser().absolute()
    target = requested.parent.resolve() / requested.name
    try:
        _cancel(observer)
        _durable_mkdir(target.parent)
        with tempfile.TemporaryDirectory(prefix=".tivelo-archive-", dir=target.parent) as temporary:
            stage = Path(temporary)
            paths = _write_inputs(stage, lock, definitions)
            rows = {}
            encoding = "parquet-v1"
            if result is not None:
                result = BacktestResult.model_validate_json(canonical_bytes(result))
                if any(order.order_type is not None for order in result.orders):
                    encoding = "parquet-v2"
                metrics = calculate_metrics(lock, result)
                _write(stage, "metrics.json", canonical_bytes(metrics))
                summary = ResultSummary.model_validate(
                    {name: getattr(result, name) for name in ResultSummary.model_fields}
                )
                _write(stage, "summary.json", canonical_bytes(summary))
                paths.extend(("summary.json", "metrics.json"))
                for path, (attribute, _, fields) in _result_tables(encoding).items():
                    _cancel(observer)
                    records = getattr(result, attribute)
                    if len(records) > MAX_TABLE_ROWS:
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID, "Result table exceeds row budget"
                        )
                    frame = pl.DataFrame(
                        [
                            {"sequence": index, **record.model_dump()}
                            for index, record in enumerate(records)
                        ],
                        schema={"sequence": pl.UInt64, **fields},
                        strict=True,
                    )
                    with (stage / path).open("xb") as output:
                        frame.write_parquet(output, compression="zstd", statistics=True)
                        output.flush()
                        os.fsync(output.fileno())
                    paths.append(path)
                    rows[path] = len(records)
            contents = ArchiveContents(
                lock_id=lock.lock_id,
                files=tuple(_record(stage, path, rows.get(path)) for path in sorted(paths)),
            )
            manifest = (
                ResultArchive.create(contents, encoding=encoding)
                if result is not None
                else RunArchive.create(contents)
            )
            if expected_archive is not None and manifest != expected_archive:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Backtest replay did not reproduce the original archive"
                )
            return _publish(stage, target, manifest, observer)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise TiveloError(ErrorCode.PUBLICATION_FAILED, "Archive publication I/O failed") from exc


def publish_run(
    lock: RunLock, definitions: CheckedDefinitions, destination, *, observer=None
) -> RunArchive:
    return _publish_archive(lock, definitions, destination, observer=observer)


def publish_result(
    lock: RunLock,
    definitions: CheckedDefinitions,
    result: BacktestResult,
    destination,
    *,
    observer=None,
    expected_archive: ResultArchive | None = None,
) -> ResultArchive:
    return _publish_archive(
        lock, definitions, destination, result, observer, expected_archive=expected_archive
    )


def _read_archive_inputs(directory, *, result=False):
    manifest, lock = inspect_archive(directory, result=result)
    root = Path(directory).expanduser().absolute()
    definitions = DefinitionLoader(root / "definitions").inspect(
        root / "definitions" / lock.contents.spec.strategy.sha256,
        expected=lock.contents.spec.strategy,
    )
    expected = {"run-lock.json"}
    if result:
        expected.update(("summary.json", "metrics.json", *_result_tables(manifest.encoding)))
    for node in definitions.nodes:
        prefix = f"definitions/{node.bundle.bundle_id}"
        expected.add(f"{prefix}/definition.json")
        expected.update(f"{prefix}/{path}" for path, _ in node.files)
    if definitions.dependencies != lock.contents.spec.dependencies or expected != {
        file.path for file in manifest.contents.files
    }:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Archive has a different definition closure"
        )
    if inspect_archive(directory, result=result)[0] != manifest:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Archive changed while capturing definitions")
    return manifest, lock, definitions


def read_run(directory) -> tuple[RunLock, CheckedDefinitions]:
    _, lock, definitions = _read_archive_inputs(directory)
    return lock, definitions


def read_result_inputs(directory) -> tuple[ResultArchive, RunLock, CheckedDefinitions]:
    """Capture a result's original lock and definition bytes for replay."""
    return _read_archive_inputs(directory, result=True)


def read_result(directory) -> tuple[RunLock, BacktestResult, RunMetrics]:
    manifest, lock = inspect_archive(directory, result=True)
    root = Path(directory).expanduser().absolute()
    try:
        summary = ResultSummary.model_validate_json(_read(root, "summary.json"))
        metrics = RunMetrics.model_validate_json(_read(root, "metrics.json"))
        inventory = {file.path: file for file in manifest.contents.files}
        values = summary.model_dump()
        for path, (attribute, model, fields) in _result_tables(manifest.encoding).items():
            expected = pl.Schema({"sequence": pl.UInt64, **fields})
            table_path = _safe_path(root, path)
            lazy = pl.scan_parquet(table_path)
            if lazy.collect_schema() != expected:
                raise ValueError("result table schema differs")
            count = lazy.select(pl.len()).collect().item()
            if count != inventory[path].row_count or count > MAX_TABLE_ROWS:
                raise ValueError("result table row count differs or exceeds budget")
            table = lazy.collect()
            records = []
            for index, record in enumerate(table.iter_rows(named=True)):
                if record.pop("sequence") != index:
                    raise ValueError("result table order differs")
                records.append(model.model_validate(record))
            values[attribute] = tuple(records)
        result = BacktestResult.model_validate(values)
        if calculate_metrics(lock, result) != metrics:
            raise ValueError("summary metrics do not match result")
        if inspect_archive(directory, result=True)[0] != manifest:
            raise ValueError("archive changed while reading")
        return lock, result, metrics
    except (OSError, ValueError, TypeError, pl.exceptions.PolarsError) as exc:
        raise TiveloError(ErrorCode.ARTIFACT_CORRUPT, "Result archive failed verification") from exc
