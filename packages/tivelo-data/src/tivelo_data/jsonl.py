"""Explicit local-file source for independent CLI/import pipelines."""

from __future__ import annotations

from pathlib import Path

from tivelo_contracts.base import Contract, Digest, fingerprint
from tivelo_contracts.data import Coverage, CoverageIssue
from tivelo_contracts.errors import ErrorCode, TiveloError

from .providers import FetchPage, ProviderCapability, ProviderDescriptor, SyncRequest
from .records import model_for
from .storage import file_digest


class _DeclaredSource(Contract):
    content_sha256: Digest
    coverage: Coverage


class JSONLinesProvider:
    def __init__(
        self,
        path: str | Path,
        request: SyncRequest,
        *,
        source_id: str,
        knowledge_basis: str,
        bar_convention_id: str | None,
        page_size: int = 1000,
        coverage: Coverage | None = None,
    ):
        if type(page_size) is not int or not 1 <= page_size <= 10000:
            raise ValueError("local page_size must be between 1 and 10000")
        self.path = Path(path).resolve(strict=True)
        self._subjects = set(request.instrument_ids) or {None}
        self._content_digest = file_digest(self.path)
        self.coverage = (
            Coverage.model_validate_json(coverage.model_dump_json())
            if coverage is not None
            else None
        )
        if self.coverage is not None and (
            self.coverage.requested_units != len(self._subjects)
            or any(
                issue.instrument_id is not None
                and issue.instrument_id not in request.instrument_ids
                for issue in self.coverage.issues
            )
        ):
            raise ValueError("Declared coverage differs from the local source request")
        self.configuration_digest = (
            fingerprint(
                _DeclaredSource(content_sha256=self._content_digest, coverage=self.coverage)
            )
            if self.coverage is not None
            else self._content_digest
        )
        self.page_size = page_size
        self.request = request
        self._identity = self._stat()
        self._count = 0
        self._seen = set()
        self._descriptor = ProviderDescriptor(
            provider_id="tivelo.local-jsonl",
            version="1",
            source_ids=(source_id,),
            capabilities=(
                ProviderCapability(
                    record_type=request.record_type,
                    market_id=request.market_id,
                    asset_class=request.asset_class,
                    timeframes=(request.timeframe,) if request.timeframe else (),
                    price_bases=(request.price_basis,) if request.price_basis else (),
                    knowledge_basis=knowledge_basis,
                    normalization_version="jsonl-v2.0",
                    bar_convention_id=bar_convention_id,
                ),
            ),
        )

    def _stat(self):
        stat = self.path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def describe(self):
        return self._descriptor

    def check_ready(self, context):
        context.check_cancelled()
        if self._stat() != self._identity or file_digest(self.path) != self._content_digest:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Local source file changed after planning")
        self._count = 0
        self._seen.clear()

    def fetch(self, request, cursor, context):
        if request != self.request or self._stat() != self._identity:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Local source changed during synchronization"
            )
        model = model_for(request.record_type)
        records = []
        with self.path.open("rb") as stream:
            stream.seek(int(cursor) if cursor else 0)
            while len(records) < self.page_size:
                context.check_cancelled()
                line = stream.readline(1_048_577)
                if not line:
                    break
                if len(line) > 1_048_576:
                    raise TiveloError(ErrorCode.CONTRACT_INVALID, "Source record exceeds 1 MiB")
                if not line.strip():
                    continue
                record = model.model_validate_json(line)
                records.append(record)
                self._count += 1
                self._seen.add(record.instrument_id)
            offset = stream.tell()
            has_more = bool(stream.read(1))
        if has_more:
            return FetchPage(tuple(records), str(offset))
        missing = self._subjects - self._seen
        if self.coverage is not None:
            if self.coverage.row_count != self._count or (
                self.coverage.status == "complete" and missing
            ):
                raise TiveloError(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "Declared coverage contradicts the supplied local records",
                )
            return FetchPage(tuple(records), None, self.coverage)
        issues = tuple(
            CoverageIssue(
                instrument_id=key, reason="missing", detail="No records in the supplied source file"
            )
            for key in sorted(missing)
        )
        status = "empty" if self._count == 0 else "partial" if missing else "complete"
        coverage = Coverage(
            status=status,
            requested_units=len(self._subjects),
            completed_units=len(self._subjects & self._seen),
            row_count=self._count,
            issues=issues
            or (
                (CoverageIssue(reason="no_trades", detail="Source file contains no records"),)
                if not self._count
                else ()
            ),
        )
        return FetchPage(tuple(records), None, coverage)

    def close(self):
        pass
