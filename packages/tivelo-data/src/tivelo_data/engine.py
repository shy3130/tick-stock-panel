"""Independent sync, validation and snapshot API."""

from __future__ import annotations

import shutil
from contextlib import ExitStack, contextmanager
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from pydantic import ValidationError
from tivelo_contracts.base import canonical_bytes, fingerprint
from tivelo_contracts.data import Artifact, Coverage, DatasetContents, DatasetScope, DatasetVersion
from tivelo_contracts.errors import ErrorCode, TiveloError

from .providers import (
    SourceResolver,
    SyncContext,
    SyncPlan,
    SyncRequest,
    close_source,
    local_context,
    plan_source,
)
from .publications import PublicationCursor, PublicationPage, read_publications
from .records import BUSINESS_KEYS, REVISION_KEYS, arrow_schema, model_for, record_table
from .storage import Candidate, SnapshotStore, file_digest


class DataEngine:
    def __init__(
        self,
        root: str | Path,
        resolver: SourceResolver | None = None,
        *,
        max_page_rows: int = 10000,
        max_pages: int = 100000,
        memory_limit: str = "512MB",
    ):
        if max_page_rows < 1 or max_pages < 1:
            raise ValueError("page budgets must be positive")
        self._root = Path(root).expanduser().resolve()
        self._store = None
        self.resolver = resolver
        self.max_page_rows = max_page_rows
        self.max_pages = max_pages
        self.memory_limit = memory_limit

    @property
    def store(self) -> SnapshotStore:
        if self._store is None:
            self._store = SnapshotStore(self._root)
        return self._store

    def plan_sync(self, request: SyncRequest) -> SyncPlan:
        if self.resolver is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "No data source resolver configured"
            )
        return plan_source(request, self.resolver.resolve(request))

    @contextmanager
    def _sync_stage(self, plan, source):
        provider = source.provider
        with ExitStack() as rollback:
            try:
                if (
                    provider.describe() != plan.provider
                    or source.configuration_digest != plan.source_configuration_digest
                    or getattr(source, "extension", None) != plan.extension
                ):
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Source selection changed after planning"
                    )
                operation, stage = self.store.new_stage()
                rollback.callback(shutil.rmtree, stage)
                yield provider, operation, stage
            except BaseException as primary:
                close_source(provider, primary=primary)
                raise
            close_source(provider)
            # A candidate can be handed to its caller only after source cleanup succeeds.
            rollback.pop_all()

    def sync(self, plan: SyncPlan, context: SyncContext | None = None) -> Candidate:
        plan = SyncPlan.model_validate_json(canonical_bytes(plan))
        context = context or local_context()
        context.check_cancelled()
        if self.resolver is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "No data source resolver configured"
            )
        source = self.resolver.resolve(plan.request)
        with self._sync_stage(plan, source) as (provider, operation, stage):
            provider.check_ready(context)
            cursor = None
            seen = set()
            chunks = []
            rows = 0
            sources = set()
            instruments = set(plan.request.instrument_ids)
            field = plan.request.extension_field
            field_digest = fingerprint(field) if field else None
            if field and field.granularity == "market":
                instruments.add(None)
            model = model_for(plan.request.record_type)
            for _page_number in range(self.max_pages):
                context.check_cancelled()
                page = provider.fetch(plan.request, cursor, context)
                context.check_cancelled()
                if not isinstance(page.records, tuple) or len(page.records) > self.max_page_rows:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Source exceeded the bounded page contract"
                    )
                for record in page.records:
                    if not isinstance(record, model):
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID, "Source returned the wrong record type"
                        )
                    if (
                        record.instrument_id not in instruments
                        or record.source_id not in plan.provider.source_ids
                    ):
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID,
                            "Source returned an undeclared instrument or provenance",
                        )
                    if field is not None and (
                        not record.matches(field, field_digest)
                        or not plan.request.start <= record.effective_at < plan.request.end
                    ):
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID,
                            "Extension observation differs from the requested field or range",
                        )
                    if plan.request.record_type == "bar" and (
                        record.price_basis != plan.request.price_basis
                        or record.timeframe != plan.request.timeframe
                        or not plan.request.start <= record.bar_start < plan.request.end
                        or not record.is_final
                    ):
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID,
                            "Bar differs from the requested basis, range or timeframe",
                        )
                    sources.add(record.source_id)
                    if plan.request.record_type == "opening_observation" and (
                        not plan.request.start <= record.event_at < plan.request.end
                        or not record.is_final
                    ):
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID,
                            "Opening fact differs from the requested range or is unfinished",
                        )
                    if plan.request.record_type in ("order_book_observation", "trade_print"):
                        if not plan.request.start <= record.event_at < plan.request.end:
                            raise TiveloError(
                                ErrorCode.CONTRACT_INVALID,
                                "Market event is outside the capture range",
                            )
                        if (
                            plan.capability.knowledge_basis != "historical_versions"
                            and record.available_at != record.ingested_at
                        ):
                            raise TiveloError(
                                ErrorCode.FUTURE_INFORMATION,
                                "Reconstructed book and trade events may only be known at ingestion",
                            )
                if page.records:
                    chunk = stage / f"chunk-{len(chunks):05d}.parquet"
                    try:
                        table = record_table(plan.request.record_type, page.records)
                    except (ValueError, TypeError) as exc:
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID, "Source returned invalid fact fields"
                        ) from exc
                    pq.write_table(table, chunk, compression="zstd", use_dictionary=False)
                    chunks.append(chunk)
                    rows += len(page.records)
                    context.progress(rows, "staging")
                if page.next_cursor is None:
                    if page.coverage is None:
                        raise TiveloError(
                            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Source omitted final coverage"
                        )
                    coverage = Coverage.model_validate_json(canonical_bytes(page.coverage))
                    if coverage.row_count != rows:
                        raise TiveloError(
                            ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                            "Source omitted or contradicted final coverage",
                        )
                    if coverage.status == "error":
                        raise TiveloError(
                            ErrorCode.SOURCE_UNAVAILABLE, "Source reported a failed request"
                        )
                    break
                if page.coverage is not None or not page.next_cursor or page.next_cursor in seen:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Invalid or cyclic source pagination"
                    )
                seen.add(page.next_cursor)
                cursor = page.next_cursor
            else:
                raise TiveloError(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "Source pagination exceeded the request budget",
                )
            context.check_cancelled()
            artifacts = ()
            if chunks:
                final = stage / "final-00000.parquet"
                self._finalize(plan.request.record_type, chunks, final, context)
                digest = file_digest(final)
                artifacts = (
                    Artifact(
                        path=f"datasets/{digest[:2]}/{digest}.parquet",
                        sha256=digest,
                        size_bytes=final.stat().st_size,
                        row_count=rows,
                    ),
                )
            contents = DatasetContents(
                dataset_id=plan.request.dataset_id,
                record_type=plan.request.record_type,
                knowledge_basis=plan.capability.knowledge_basis,
                price_basis=plan.request.price_basis,
                source_ids=tuple(sorted(sources or set(plan.provider.source_ids))),
                artifacts=artifacts,
                coverage=coverage,
                normalization_version=plan.capability.normalization_version,
                source_selection_digest=fingerprint(plan),
                scope=DatasetScope(
                    market_id=plan.request.market_id,
                    asset_class=plan.request.asset_class,
                    instrument_ids=tuple(sorted(plan.request.instrument_ids)),
                    extension_field=field,
                    start=plan.request.start,
                    end=plan.request.end,
                    timeframes=(plan.request.timeframe,) if plan.request.timeframe else (),
                    bar_convention_id=plan.capability.bar_convention_id,
                ),
            )
            candidate = Candidate(candidate_id=operation, version=DatasetVersion.create(contents))
            context.check_cancelled()
            self.store.save_candidate(candidate)
            return candidate

    def _finalize(
        self, record_type: str, chunks: list[Path], final: Path, context: SyncContext
    ) -> None:
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("SET memory_limit = ?", [self.memory_limit])
            connection.execute("SET threads = 1")
            connection.execute("SET temp_directory = ?", [str(final.parent / "sort-spill")])
            paths = [str(path) for path in chunks]
            # All identifiers below come from our registered schema, never SQL from a provider.
            for keys in (REVISION_KEYS[record_type], (*BUSINESS_KEYS[record_type], "available_at")):
                group = ", ".join(f'"{key}"' for key in keys)
                if connection.execute(
                    f"SELECT 1 FROM read_parquet(?) GROUP BY {group} HAVING count(*) > 1 LIMIT 1",
                    [paths],
                ).fetchone():
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Source returned duplicate or ambiguous revisions"
                    )
            source_keys = ", ".join(
                f'"{key}"' for key in BUSINESS_KEYS[record_type] if key != "source_id"
            )
            if connection.execute(
                f"SELECT 1 FROM read_parquet(?) GROUP BY {source_keys} HAVING count(DISTINCT source_id)>1 LIMIT 1",
                [paths],
            ).fetchone():
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Source selection is ambiguous for a fact identity"
                )
            context.check_cancelled()
            order = ", ".join(f'"{key}"' for key in REVISION_KEYS[record_type])
            batches = connection.execute(
                f"SELECT * FROM read_parquet(?) ORDER BY {order}", [paths]
            ).to_arrow_reader(65536)
            schema = arrow_schema(record_type)
            with pq.ParquetWriter(
                final, schema, compression="zstd", use_dictionary=False, version="2.6"
            ) as writer:
                for batch in batches:
                    context.check_cancelled()
                    writer.write_batch(batch.cast(schema), row_group_size=65536)
        finally:
            connection.close()

    def validate(self, candidate: Candidate) -> DatasetVersion:
        self.store.validate_candidate_files(candidate)
        for index, artifact in enumerate(candidate.version.contents.artifacts):
            file = self.store.candidate_file(candidate, index)
            parquet = pq.ParquetFile(file)
            if parquet.metadata.num_rows != artifact.row_count or not parquet.schema_arrow.equals(
                arrow_schema(candidate.version.contents.record_type), check_metadata=True
            ):
                raise TiveloError(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    "Physical schema or row count differs from the manifest",
                )
        # Round-trip the manifest to validate all scalar and cross-field constraints.
        try:
            return DatasetVersion.model_validate_json(canonical_bytes(candidate.version))
        except ValidationError as exc:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid dataset manifest") from exc

    def publish(self, candidate: Candidate) -> DatasetVersion:
        self.validate(candidate)
        return self.store.publish(candidate)

    def publications(
        self,
        *,
        after: PublicationCursor | None = None,
        dataset_id: str | None = None,
        limit: int = 100,
        tail: bool = False,
    ) -> PublicationPage:
        """Read committed versions in order, or establish a checkpoint for future commits."""
        return read_publications(
            self.store, after=after, dataset_id=dataset_id, limit=limit, tail=tail
        )

    def create_snapshot(self, version_ids: tuple[str, ...]):
        return self.store.create_snapshot(version_ids)

    def open_reader(self, snapshot_id: str):
        from .reader import SnapshotReader

        if not (self._root / "index.sqlite3").is_file():
            raise TiveloError(ErrorCode.SNAPSHOT_NOT_FOUND, "Data store does not exist")
        return SnapshotReader(self.store, snapshot_id, memory_limit=self.memory_limit)

    def export_snapshot(self, snapshot_id: str, destination: str | Path, *, observer=None):
        from .export import export_snapshot

        return export_snapshot(self, snapshot_id, destination, observer=observer)
