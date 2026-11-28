"""Classify legacy decimal failures and audit immutable daily source bytes."""

import hashlib
from collections import Counter
from datetime import date
from decimal import Decimal, Overflow, Underflow
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, TypeAdapter, ValidationError, model_validator
from tivelo_contracts.base import CalendarDate, Contract, Digest, ExactDecimal, PositiveDecimal
from tivelo_contracts.errors import ErrorCode, TiveloError

from .adapters.v1 import legacy_decimal, scale_legacy_quantity
from .legacy.repository_files import DAILY_TABLES, changed, date_partition_files, verified_parquet
from .providers import local_context
from .repository_diagnostics import (
    IssueReason,
    Layout,
    RepositoryDecimalIssue,
    SourceColumn,
    SourceSymbol,
)
from .storage import file_digest

_EXACT = TypeAdapter(ExactDecimal)
_SYMBOL = TypeAdapter(SourceSymbol)


def row_precision_issues(exc: ValidationError, layout, raw_prefix, day, row, digest):
    """Map actual Bar validation failures back to their physical input columns."""
    issues = []
    for detail in exc.errors(include_url=False, include_context=False):
        if len(detail["loc"]) != 1:
            continue
        field = detail["loc"][0]
        if field in ("open", "high", "low", "close"):
            field = raw_prefix + field
        elif field == "turnover":
            field = "amount"
        elif field != "volume":
            continue
        # Bar may also reject a value for its sign or an OHLC rule. Only attach
        # a precision diagnostic when the installed public decimal type rejects it.
        reason = _numeric_issue(detail["input"])
        if reason is None:
            continue
        issues.append(
            RepositoryDecimalIssue(
                layout=layout,
                trading_date=day,
                symbol=row["symbol"],
                field=field,
                row_number=row["_source_row"],
                file_sha256=digest,
                reason=reason,
            )
        )
    return tuple(issues)


class RepositoryPrecisionRequest(Contract):
    layout: Layout
    start: CalendarDate
    end: CalendarDate
    fields: tuple[SourceColumn, ...] = Field(
        default=("open", "high", "low", "close", "volume", "amount"), min_length=1, max_length=10
    )
    symbols: tuple[SourceSymbol, ...] = Field(default=(), max_length=10000)
    volume_multiplier: PositiveDecimal = "1"
    max_issues: int = Field(default=100, strict=True, ge=0, le=10000)

    @model_validator(mode="after")
    def valid_selection(self) -> Self:
        if (
            self.end < self.start
            or len(set(self.fields)) != len(self.fields)
            or len(set(self.symbols)) != len(self.symbols)
        ):
            raise ValueError("Invalid daily precision audit range or duplicate selectors")
        return self


class RepositoryPrecisionReport(Contract):
    format: Literal["tivelo-repository-decimal-audit-v1"] = "tivelo-repository-decimal-audit-v1"
    request: RepositoryPrecisionRequest
    source_sha256: Digest
    files: int
    checked_rows: int
    checked_values: int
    affected_rows: int
    issue_count: int
    by_field: dict[SourceColumn, int]
    by_reason: dict[IssueReason, int]
    issues: tuple[RepositoryDecimalIssue, ...]
    truncated: bool
    status: Literal["empty", "clean", "invalid"]


def decimal_precision_reason(
    number: Decimal,
) -> Literal["integral_range", "fractional_precision"] | None:
    """Classify a finite source decimal against the published exact-value contract."""
    try:
        _EXACT.validate_python(number)
    except ValidationError:
        return "integral_range" if number.adjusted() >= 26 else "fractional_precision"
    return None


def _numeric_issue(value, multiplier: Decimal | int = 1) -> IssueReason | None:
    try:
        number = legacy_decimal(value)
        if multiplier != 1:
            number = scale_legacy_quantity(number, multiplier)
    except Overflow:
        return "integral_range"
    except Underflow:
        return "fractional_precision"
    except TiveloError:
        return "invalid_number"
    return decimal_precision_reason(number)


def audit_repository_precision(
    root: Path, request: RepositoryPrecisionRequest, *, context=None
) -> RepositoryPrecisionReport:
    import pyarrow as pa

    context = context or local_context()
    context.check_cancelled()
    table = Path(root).resolve(strict=True) / DAILY_TABLES[request.layout]
    if not table.is_dir() or table.is_symlink():
        raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "Legacy daily table is unavailable")
    inventory = date_partition_files(table, request.start, request.end)
    source_hash = hashlib.sha256()
    selected = set(request.symbols)
    by_field, by_reason = Counter(), Counter()
    checked_rows = checked_values = affected = 0
    issues = []
    try:
        for path in sorted(inventory):
            context.check_cancelled()
            digest = file_digest(path)
            source_hash.update(
                path.relative_to(table.parent).as_posix().encode() + b"\0" + bytes.fromhex(digest)
            )
            day = date.fromisoformat(path.parent.name.removeprefix("date="))
            with verified_parquet(path, digest, context) as parquet:
                present = set(parquet.schema_arrow.names)
                if "symbol" not in present or set(request.fields) - present - {"amount"}:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID,
                        "Requested numeric column missing from daily partition",
                    )
                columns = [field for field in request.fields if field in present]
                ordinal = 0
                for batch in parquet.iter_batches(columns=["symbol", *columns], batch_size=2048):
                    context.check_cancelled()
                    for row in batch.to_pylist():
                        ordinal += 1
                        symbol = row["symbol"]
                        if selected and symbol not in selected:
                            continue
                        checked_rows += 1
                        failed = False
                        for field in columns:
                            if field == "amount" and row[field] is None:
                                continue
                            checked_values += 1
                            reason = _numeric_issue(
                                row[field], request.volume_multiplier if field == "volume" else 1
                            )
                            if reason is None:
                                continue
                            if not failed:
                                _SYMBOL.validate_python(symbol)
                                failed = True
                            by_field[field] += 1
                            by_reason[reason] += 1
                            if len(issues) < request.max_issues:
                                issues.append(
                                    RepositoryDecimalIssue(
                                        layout=request.layout,
                                        trading_date=day,
                                        symbol=symbol,
                                        field=field,
                                        row_number=ordinal,
                                        file_sha256=digest,
                                        reason=reason,
                                    )
                                )
                        affected += int(failed)
                    context.progress(checked_rows, "checking_decimal_precision")
    except FileNotFoundError:
        raise changed() from None
    except (pa.ArrowException, TypeError, ValueError):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Invalid legacy daily precision input"
        ) from None
    context.check_cancelled()
    if date_partition_files(table, request.start, request.end) != inventory:
        raise changed()
    count = sum(by_field.values())
    return RepositoryPrecisionReport(
        request=request,
        source_sha256=source_hash.hexdigest(),
        files=len(inventory),
        checked_rows=checked_rows,
        checked_values=checked_values,
        affected_rows=affected,
        issue_count=count,
        by_field=dict(by_field),
        by_reason=dict(by_reason),
        issues=tuple(issues),
        truncated=count > len(issues),
        status="invalid" if count else "clean" if checked_values else "empty",
    )
