"""Read immutable Parquet versions through bounded, as-of SQL projections."""

from __future__ import annotations

import errno
import tempfile
from contextlib import ExitStack
from glob import escape
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from tivelo_contracts.base import utc_datetime
from tivelo_contracts.data import BarQuery, EventQuery, HistoryQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.extension_fields import ExtensionQuery, resolve_field

from .records import BUSINESS_KEYS, arrow_schema, model_for
from .storage import SnapshotStore, file_digest


class SnapshotReader:
    def __init__(self, store: SnapshotStore, snapshot_id: str, *, memory_limit: str = "512MB"):
        self._store = store
        self._reference = ExitStack()
        self._memory_limit = memory_limit
        self._closed = False
        self._verified = {}
        self._queries: set[ExitStack] = set()
        self._manifest = self._reference.enter_context(store.reader_reference(snapshot_id))
        try:
            self._datasets = tuple(
                store.dataset_version(ref.version_id) for ref in self._manifest.contents.datasets
            )
            for ref, version in zip(self._manifest.contents.datasets, self._datasets, strict=True):
                if ref.dataset_id != version.contents.dataset_id:
                    raise TiveloError(
                        ErrorCode.SNAPSHOT_CORRUPT, "Snapshot has an incorrect dataset identity"
                    )
        except BaseException:
            self._reference.close()
            raise

    @property
    def manifest(self):
        return self._manifest

    @property
    def datasets(self):
        return self._datasets

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._closed:
            return
        try:
            for resources in tuple(self._queries):
                resources.close()
        finally:
            self._queries.clear()
            self._closed = True
            self._verified.clear()
            self._reference.close()

    def verify(self):
        """Verify every referenced physical artifact without materializing facts."""
        if self._closed:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Snapshot reader is closed")
        for record_type, price_basis in {
            (version.contents.record_type, version.contents.price_basis)
            for version in self._datasets
        }:
            self._files(record_type, price_basis)

    def _files(self, record_type: str, price_basis: str | None = None, dataset=None) -> list[str]:
        if self._closed:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Snapshot reader is closed")
        files = []
        for version in (dataset,) if dataset is not None else self._datasets:
            if (
                version.contents.record_type != record_type
                or version.contents.price_basis != price_basis
            ):
                continue
            for artifact in version.contents.artifacts:
                path = self._store.path(artifact.path)
                try:
                    stat = path.stat()
                    identity = (
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                    )
                    if self._verified.get(path) != identity:
                        if (
                            stat.st_size != artifact.size_bytes
                            or file_digest(path) != artifact.sha256
                        ):
                            raise TiveloError(
                                ErrorCode.SNAPSHOT_CORRUPT, "Snapshot fact hash differs"
                            )
                        parquet = pq.ParquetFile(path)
                        if (
                            parquet.metadata.num_rows != artifact.row_count
                            or not parquet.schema_arrow.equals(
                                arrow_schema(record_type), check_metadata=True
                            )
                        ):
                            raise TiveloError(
                                ErrorCode.SNAPSHOT_CORRUPT, "Snapshot physical schema differs"
                            )
                        self._verified[path] = identity
                except OSError as exc:
                    raise TiveloError(
                        ErrorCode.SNAPSHOT_CORRUPT, "Snapshot fact file is unavailable"
                    ) from exc
                # DuckDB expands glob characters even in bound file arguments.
                files.append(escape(path.as_posix()))
        return sorted(set(files))

    def _batches(
        self,
        record_type,
        as_of,
        before="TRUE",
        before_args=(),
        after="TRUE",
        after_args=(),
        *,
        price_basis=None,
        dataset=None,
        batch_size=65536,
    ):
        if not 1 <= batch_size <= 65536:
            raise ValueError("batch_size must be between 1 and 65536")
        files = self._files(record_type, price_basis, dataset)
        if not files:
            return
        keys = tuple(key for key in BUSINESS_KEYS[record_type] if key != "source_id")
        partition = ", ".join(f'"{key}"' for key in keys)
        common = (
            f"WITH visible AS (SELECT DISTINCT * FROM read_parquet(?) WHERE available_at <= ? AND ({before})), "
            f"ranked AS (SELECT *, dense_rank() OVER (PARTITION BY {partition} ORDER BY available_at DESC) AS _revision_rank FROM visible) "
        )
        order = '"bar_start", "instrument_id", "timeframe"' if record_type == "bar" else partition
        visible = "SELECT * EXCLUDE (_revision_rank) FROM ranked WHERE _revision_rank=1"
        if record_type == "extension_value":
            visible += " QUALIFY effective_at=max(effective_at) OVER (PARTITION BY instrument_id)"
        sql = common + f"SELECT * FROM ({visible}) WHERE ({after}) ORDER BY {order}"
        temporary_root = self._store.path("staging")
        resources = ExitStack()
        self._queries.add(resources)
        spill_directory = ""
        try:
            try:
                temporary_root.mkdir(parents=True, exist_ok=True)
                spill = resources.enter_context(
                    tempfile.TemporaryDirectory(prefix="reader-", dir=temporary_root)
                )
                spill_directory = str(Path(spill) / "spill")
            except OSError as exc:
                if exc.errno not in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                    raise
                # A pinned snapshot can still be queried within its memory budget.
            connection = duckdb.connect(":memory:")
            resources.callback(connection.close)
            connection.execute("SET memory_limit = ?", [self._memory_limit])
            connection.execute("SET TimeZone = 'UTC'")
            connection.execute("SET temp_directory = ?", [spill_directory])
            if connection.execute(
                common
                + f"SELECT 1 FROM ranked WHERE _revision_rank=1 GROUP BY {partition} HAVING count(*)>1 LIMIT 1",
                [files, as_of, *before_args],
            ).fetchone():
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Snapshot contains conflicting visible revisions"
                )
            result = connection.execute(
                sql, [files, as_of, *before_args, *after_args]
            ).to_arrow_reader(batch_size)
            for batch in result:
                if self._closed:
                    raise TiveloError(ErrorCode.CONTRACT_INVALID, "Snapshot reader is closed")
                yield batch.cast(arrow_schema(record_type))
        except duckdb.OutOfMemoryException as exc:
            if spill_directory:
                raise
            raise TiveloError(
                ErrorCode.RESOURCE_BUSY,
                "Query exceeds its memory budget while the data volume is full",
            ) from exc
        finally:
            resources.close()
            self._queries.discard(resources)

    def _query(
        self,
        record_type,
        as_of,
        before="TRUE",
        before_args=(),
        after="TRUE",
        after_args=(),
        *,
        price_basis=None,
        dataset=None,
    ):
        model = model_for(record_type)
        return tuple(
            model.model_validate(row)
            for batch in self._batches(
                record_type,
                as_of,
                before,
                before_args,
                after,
                after_args,
                price_basis=price_basis,
                dataset=dataset,
            )
            for row in batch.to_pylist()
        )

    def iter_bar_batches(self, query: BarQuery, *, batch_size: int = 65536):
        yield from self._batches(
            "bar",
            query.as_of,
            "instrument_id IN (SELECT unnest(?)) AND timeframe=? AND bar_start>=? AND bar_start<?",
            (list(query.instrument_ids), query.timeframe, query.start, query.end),
            "is_final AND bar_end<=?",
            (query.as_of,),
            price_basis=query.price_basis,
            batch_size=batch_size,
        )

    def bars(self, query: BarQuery):
        return self._query(
            "bar",
            query.as_of,
            "instrument_id IN (SELECT unnest(?)) AND timeframe=? AND bar_start>=? AND bar_start<?",
            (list(query.instrument_ids), query.timeframe, query.start, query.end),
            "is_final AND bar_end<=?",
            (query.as_of,),
            price_basis=query.price_basis,
        )

    def openings(self, query: EventQuery):
        return self._query(
            "opening_observation",
            query.as_of,
            "instrument_id IN (SELECT unnest(?)) AND event_at>=? AND event_at<?",
            (list(query.instrument_ids), query.start, query.end),
            "is_final AND event_at<=?",
            (query.as_of,),
        )

    def order_books(self, query):
        if "order_book_observation" not in BUSINESS_KEYS:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Order-book contracts are unavailable"
            )
        from tivelo_contracts.order_books import select_order_book_dataset

        dataset = select_order_book_dataset(self.datasets, query.dataset)
        return self._query(
            "order_book_observation",
            query.as_of,
            "instrument_id IN (SELECT unnest(?)) AND event_at>=? AND event_at<?",
            (list(query.instrument_ids), query.start, query.end),
            dataset=dataset,
        )

    def trade_prints(self, query):
        if "trade_print" not in BUSINESS_KEYS:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Trade-print contracts are unavailable"
            )
        from tivelo_contracts.trade_prints import select_trade_dataset

        dataset = select_trade_dataset(self.datasets, query.dataset)
        before = "instrument_id IN (SELECT unnest(?)) AND trading_date=?"
        args = (list(query.instrument_ids), query.trading_date)
        if query.sequence is not None:
            before += " AND sequence=?"
            args = (*args, query.sequence)
        return self._query(
            "trade_print",
            query.as_of,
            before,
            args,
            "event_at>=? AND event_at<?",
            (query.start, query.end),
            dataset=dataset,
        )

    def extension_values(self, query: ExtensionQuery):
        dataset = resolve_field(self.datasets, query, query.instrument_ids)
        predicate = (
            "instrument_id IN (SELECT unnest(?))"
            if query.instrument_ids
            else "instrument_id IS NULL"
        )
        subjects = (list(query.instrument_ids),) if query.instrument_ids else ()
        return self._query(
            "extension_value",
            query.as_of,
            predicate + " AND effective_at<=?",
            (*subjects, query.effective_at),
            "valid_until IS NULL OR valid_until>?",
            (query.effective_at,),
            dataset=dataset,
        )

    def instruments(self, query: HistoryQuery):
        result = self._query(
            "instrument",
            query.as_of,
            "instrument_id IN (SELECT unnest(?))",
            (list(query.instrument_ids),),
            "valid_from<=? AND (valid_to IS NULL OR ?<valid_to)",
            (query.effective_at, query.effective_at),
        )
        if len({record.instrument_id for record in result}) != len(result):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Overlapping active instrument specifications"
            )
        return result

    def actions(self, query: HistoryQuery):
        return self._query(
            "corporate_action",
            query.as_of,
            "instrument_id IN (SELECT unnest(?))",
            (list(query.instrument_ids),),
            "effective_at<=? AND NOT cancelled",
            (query.effective_at,),
        )

    def factors(self, query: HistoryQuery):
        return self._query(
            "adjustment_factor",
            query.as_of,
            "instrument_id IN (SELECT unnest(?))",
            (list(query.instrument_ids),),
            "effective_at<=? AND NOT cancelled",
            (query.effective_at,),
        )

    def resolve_symbol(self, symbol, *, source_id, venue_id, effective_at, as_of):
        result = self._query(
            "symbol_mapping",
            utc_datetime(as_of),
            "source_id=?",
            (source_id,),
            "symbol=? AND venue_id=? AND valid_from<=? AND (valid_to IS NULL OR ?<valid_to)",
            (symbol, venue_id, utc_datetime(effective_at), utc_datetime(effective_at)),
        )
        if not result:
            raise TiveloError(ErrorCode.INSTRUMENT_NOT_FOUND, "No visible symbol mapping")
        if len(result) != 1:
            raise TiveloError(ErrorCode.AMBIGUOUS_INSTRUMENT, "Symbol maps to multiple instruments")
        return result[0].instrument_id
