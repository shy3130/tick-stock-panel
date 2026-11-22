"""Read-only portable snapshot adapter, without a Data engine installation.

This adapter owns no synchronization, index, provider routing or data repair.
It verifies the public Parquet contract, then applies the same as-of semantics
as DataReader. Queries materialize selected revisions; this is a correctness
path, not the optimized matrix preparation path.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import polars as pl
from pydantic import ValidationError
from tivelo_contracts.artifacts import MAX_SNAPSHOT_BUNDLE_BYTES, SnapshotBundle
from tivelo_contracts.base import fingerprint, utc_datetime
from tivelo_contracts.data import BarQuery, EventQuery, HistoryQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.extension_fields import ExtensionQuery, resolve_field
from tivelo_contracts.facts import BUSINESS_KEYS, RECORD_MODELS, FactField, fact_fields


def _dtype(field: FactField):
    if field.kind == "struct":
        return pl.Struct([pl.Field(child.name, _dtype(child)) for child in field.children])
    return {
        "string": pl.String,
        "int64": pl.Int64,
        "bool": pl.Boolean,
        "decimal128": pl.Decimal(38, 12),
        "timestamp": pl.Datetime("us", "UTC"),
        "date": pl.Date,
    }[field.kind]


def _required_nulls(field: FactField, expression: pl.Expr) -> pl.Expr:
    invalid = expression.is_null() if not field.nullable else pl.lit(False)
    for child in field.children:
        invalid |= expression.is_not_null() & _required_nulls(
            child, expression.struct.field(child.name)
        )
    return invalid


def _identity(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Bundle fact is not a regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class PortableSnapshotReader:
    def __init__(self, root: str | Path, *, snapshot_id: str | None = None):
        path = Path(root).expanduser().absolute()
        self._root = path.parent.resolve() / path.name
        self._closed = False
        self._verified = {}
        self._prepared = {}
        self._prepared_bytes = 0
        try:
            marker = self._path("bundle.json")
            if _identity(marker)[2] > MAX_SNAPSHOT_BUNDLE_BYTES:
                raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Bundle manifest exceeds size limit")
            self._bundle = SnapshotBundle.model_validate_json(marker.read_bytes())
        except FileNotFoundError as exc:
            raise TiveloError(ErrorCode.SNAPSHOT_NOT_FOUND, "Bundle is not published") from exc
        except (OSError, ValidationError) as exc:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Bundle manifest failed verification"
            ) from exc
        if snapshot_id is not None and self.manifest.snapshot_id != snapshot_id:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Bundle differs from the requested snapshot")
        self.verify()

    @property
    def manifest(self):
        return self._bundle.snapshot

    @property
    def datasets(self):
        return self._bundle.datasets

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._closed = True
        self._verified.clear()
        self._prepared.clear()
        self._prepared_bytes = 0

    @property
    def prepared_fact_bytes(self):
        """Retained Polars buffers, not process RSS or transient decoding memory."""
        return self._prepared_bytes

    def prepare(self, *, max_bytes: int, observer=None):
        """Load verified facts once for serial trials; queries keep their as-of filters.

        Buffers are private and shared by lazy queries, not copied per trial. The
        byte budget covers retained fact buffers; worker RSS limits are separate.
        Failed/cancelled preparation publishes no partial cache.
        """
        self._check_open()
        if type(max_bytes) is not int or max_bytes < 1:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid prepared-fact byte budget")
        if observer is not None and observer.cancelled():
            raise TiveloError(ErrorCode.CANCELLED, "Fact preparation cancelled")
        if self._prepared:
            self.verify()
            if any(_identity(path) != value[0] for path, value in self._prepared.items()):
                raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Prepared fact identity changed")
            if self._prepared_bytes > max_bytes:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Prepared facts exceed the byte budget"
                )
            return self._prepared_bytes
        files = {}
        for kind, basis in {
            (v.contents.record_type, v.contents.price_basis) for v in self.datasets
        }:
            files.update(self._files(kind, basis))
        prepared, size, complete = {}, 0, False
        chunks, frame = [], None
        try:
            for index, (path, identity) in enumerate(sorted(files.items())):
                if observer is not None and observer.cancelled():
                    raise TiveloError(ErrorCode.CANCELLED, "Fact preparation cancelled")
                chunks = []
                aborted = None

                def consume(batch, chunks=chunks):
                    nonlocal size, aborted
                    if aborted is not None:
                        return True
                    if observer is not None and observer.cancelled():
                        aborted = TiveloError(ErrorCode.CANCELLED, "Fact preparation cancelled")
                        return True
                    projected = size + batch.estimated_size()
                    if projected > max_bytes:
                        aborted = TiveloError(
                            ErrorCode.CONTRACT_INVALID, "Prepared facts exceed the byte budget"
                        )
                        return True
                    size = projected
                    chunks.append(batch)
                    return False

                # The synchronous sink's public stop signal also joins execution;
                # the installed collect_batches iterator exposes no close/stop API.
                pl.scan_parquet(path, glob=False, hive_partitioning=False).sink_batches(
                    consume, chunk_size=4096, maintain_order=True, engine="streaming"
                )
                if aborted is not None:
                    raise aborted
                frame = (
                    pl.concat(chunks, rechunk=False)
                    if chunks
                    else pl.DataFrame(schema=pl.read_parquet_schema(path))
                )
                if self._path(path.relative_to(self._root)) != path or _identity(path) != identity:
                    raise TiveloError(
                        ErrorCode.SNAPSHOT_CORRUPT, "Snapshot fact changed during preparation"
                    )
                prepared[path] = identity, frame
                if observer is not None:
                    observer.progress(index + 1, len(files), "prepare_facts")
            if observer is not None and observer.cancelled():
                raise TiveloError(ErrorCode.CANCELLED, "Fact preparation cancelled")
            if any(
                self._path(path.relative_to(self._root)) != path or _identity(path) != identity
                for path, identity in files.items()
            ):
                raise TiveloError(
                    ErrorCode.SNAPSHOT_CORRUPT, "Snapshot changed before preparation completed"
                )
            complete = True
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Fact preparation failed") from exc
        finally:
            # Do not retain large partial buffers in a caught exception traceback.
            chunks.clear()
            frame = None
            if not complete:
                prepared.clear()
        self._prepared, self._prepared_bytes = prepared, size
        return size

    def _check_open(self):
        if self._closed:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Snapshot reader is closed")

    def _path(self, relative):
        if self._root.is_symlink():
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Bundle root is a symbolic link")
        path = self._root
        for part in Path(relative).parts:
            path /= part
            if path.is_symlink():
                raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Bundle contains a symbolic link")
        return path

    def _verify_file(self, version, artifact):
        path = self._path(artifact.path)
        identity = _identity(path)
        key = (path, version.version_id)
        if self._verified.get(key) == identity:
            return path, identity
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if identity[2] != artifact.size_bytes or digest != artifact.sha256:
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Snapshot fact hash differs")
        fields = fact_fields(version.contents.record_type)
        expected = [(field.name, _dtype(field)) for field in fields]
        metadata = pl.read_parquet_metadata(path)
        if list(pl.read_parquet_schema(path).items()) != expected or any(
            metadata.get(key) != value
            for key, value in {
                "tivelo.schema": "2.0",
                "tivelo.record_type": version.contents.record_type,
                "tivelo.timezone": "UTC",
                "tivelo.decimal": "decimal128(38,12)",
            }.items()
        ):
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Snapshot physical schema differs")
        invalid = pl.any_horizontal(
            [_required_nulls(field, pl.col(field.name)) for field in fields]
        )
        if any(field.name == "schema_version" for field in fields):
            invalid |= pl.col("schema_version") != "2.0"
        invalid |= ~pl.col("source_id").is_in(version.contents.source_ids)
        if version.contents.price_basis is not None:
            invalid |= pl.col("price_basis") != version.contents.price_basis
        scope = version.contents.scope
        if version.contents.record_type in ("order_book_observation", "trade_print"):
            invalid |= (
                (pl.col("price_basis") != "raw")
                | (pl.col("event_at") > pl.col("available_at"))
                | (pl.col("available_at") > pl.col("ingested_at"))
                | (pl.col("event_at") < scope.start)
                | (pl.col("event_at") >= scope.end)
            )
            if version.contents.knowledge_basis != "historical_versions":
                invalid |= pl.col("available_at") != pl.col("ingested_at")
        if version.contents.record_type == "trade_print":
            invalid |= (
                (pl.col("raw_price") <= 0)
                | (pl.col("quantity") <= 0)
                | (pl.col("sequence") < 0)
                | ~pl.col("aggressor_side").is_in(["buy", "sell", "unknown"])
                | ~pl.col("quantity_unit").is_in(["share", "contract", "unit"])
            )
        if version.contents.record_type == "order_book_observation":
            bid, ask, trade, trade_at = (
                pl.col(name)
                for name in (
                    "best_bid",
                    "best_ask",
                    "last_trade",
                    "last_trade_at",
                )
            )
            state = pl.col("state")
            invalid |= (
                ~state.is_in(["continuous", "halted"])
                | (trade_at > pl.col("event_at")).fill_null(False)
                | (trade_at.is_null() != trade.is_null())
                | ((state == "continuous") & (bid >= ask)).fill_null(False)
                | pl.any_horizontal([(price <= 0).fill_null(False) for price in (bid, ask, trade)])
            )
        if scope is not None:
            field = scope.extension_field
            if field and field.granularity == "market":
                invalid |= pl.col("instrument_id").is_not_null()
            else:
                invalid |= ~pl.col("instrument_id").is_in(scope.instrument_ids).fill_null(False)
            if field:
                invalid |= (
                    (pl.col("field_sha256") != fingerprint(field))
                    | (pl.col("ingested_at") < pl.col("available_at"))
                    | (pl.col("effective_at") < scope.start)
                    | (pl.col("effective_at") >= scope.end)
                    | (pl.col("valid_until") <= pl.col("effective_at")).fill_null(False)
                )
                for dtype, name in (
                    ("int", "int_value"),
                    ("float", "decimal_value"),
                    ("bool", "bool_value"),
                    ("string", "text_value"),
                ):
                    if dtype != field.value_type:
                        invalid |= pl.col(name).is_not_null()
        counts = (
            pl.scan_parquet(path, glob=False, hive_partitioning=False)
            .select(pl.len().alias("rows"), invalid.any().alias("invalid"))
            .collect(engine="streaming")
        )
        if counts["rows"][0] != artifact.row_count or counts["invalid"][0]:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Snapshot facts contradict their schema or manifest"
            )
        if _identity(path) != identity:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Snapshot fact changed during verification"
            )
        self._verified[key] = identity
        return path, identity

    def _files(self, record_type, price_basis, dataset=None):
        self._check_open()
        files = {}
        try:
            for version in (dataset,) if dataset is not None else self.datasets:
                if (
                    version.contents.record_type != record_type
                    or version.contents.price_basis != price_basis
                ):
                    continue
                for artifact in version.contents.artifacts:
                    path, identity = self._verify_file(version, artifact)
                    files[path] = identity
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Snapshot fact failed verification"
            ) from exc
        return files

    def verify(self):
        self._check_open()
        for kind, basis in {
            (v.contents.record_type, v.contents.price_basis) for v in self.datasets
        }:
            self._files(kind, basis)

    def _query(self, record_type, as_of, before, after, *, price_basis=None, dataset=None):
        return tuple(self._iter_query(
            record_type, as_of, before, after, price_basis=price_basis, dataset=dataset
        ))

    def _iter_query(self, record_type, as_of, before, after, *, price_basis=None, dataset=None, keep_effective_history=False):
        files = self._files(record_type, price_basis, dataset)
        if not files:
            return
        keys = [key for key in BUSINESS_KEYS[record_type] if key != "source_id"]
        try:
            if self._prepared:
                if any(
                    path not in self._prepared or self._prepared[path][0] != identity
                    for path, identity in files.items()
                ):
                    raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Prepared fact identity changed")
                source = pl.concat(
                    [self._prepared[path][1].lazy() for path in files], rechunk=False
                )
            else:
                source = pl.scan_parquet(list(files), glob=False, hive_partitioning=False)
            latest = (
                source.filter((pl.col("available_at") <= as_of) & before)
                .unique()
                .with_columns(
                    pl.col("available_at").rank("dense", descending=True).over(keys).alias("_rank")
                )
                .filter(pl.col("_rank") == 1)
                .drop("_rank")
            )
            if record_type == "extension_value" and not keep_effective_history:
                latest = latest.filter(
                    pl.col("effective_at") == pl.col("effective_at").max().over("instrument_id")
                )
            latest = latest.collect(engine="streaming")
            for path, identity in files.items():
                if self._path(path.relative_to(self._root)) != path or _identity(path) != identity:
                    raise TiveloError(
                        ErrorCode.SNAPSHOT_CORRUPT, "Snapshot fact changed during query"
                    )
            # Detect ambiguity before cancellation/effective/final filtering so a
            # hidden conflicting revision cannot make an old fact valid again.
            if latest.group_by(keys).len().filter(pl.col("len") > 1).height:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Snapshot contains conflicting visible revisions"
                )
            order = ["bar_start", "instrument_id", "timeframe"] if record_type == "bar" else keys
            rows = latest.filter(after).sort(order)
            model = RECORD_MODELS[record_type]
            yield from (model.model_validate(row) for row in rows.iter_rows(named=True))
        except (OSError, pl.exceptions.PolarsError, ValidationError) as exc:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Snapshot query failed fact validation"
            ) from exc

    def bars(self, query: BarQuery):
        return tuple(self.iter_bars(query))

    def iter_bars(self, query: BarQuery):
        """Yield validated visible bars in reader order within the open reader context."""
        return self._iter_query(
            "bar",
            query.as_of,
            pl.col("instrument_id").is_in(query.instrument_ids)
            & (pl.col("timeframe") == query.timeframe)
            & (pl.col("bar_start") >= query.start)
            & (pl.col("bar_start") < query.end),
            pl.col("is_final") & (pl.col("bar_end") <= query.as_of),
            price_basis=query.price_basis,
        )

    def openings(self, query: EventQuery):
        return self._query(
            "opening_observation",
            query.as_of,
            pl.col("instrument_id").is_in(query.instrument_ids)
            & (pl.col("event_at") >= query.start)
            & (pl.col("event_at") < query.end),
            pl.col("is_final") & (pl.col("event_at") <= query.as_of),
        )

    def order_books(self, query):
        if "order_book_observation" not in BUSINESS_KEYS:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Order-book contracts are unavailable"
            )
        from tivelo_contracts.order_books import select_order_book_dataset

        return self._query(
            "order_book_observation",
            query.as_of,
            pl.col("instrument_id").is_in(query.instrument_ids)
            & (pl.col("event_at") >= query.start)
            & (pl.col("event_at") < query.end),
            pl.lit(True),
            dataset=select_order_book_dataset(self.datasets, query.dataset),
        )

    def trade_prints(self, query):
        if "trade_print" not in BUSINESS_KEYS:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Trade-print contracts are unavailable"
            )
        from tivelo_contracts.trade_prints import select_trade_dataset

        before = pl.col("instrument_id").is_in(query.instrument_ids) & (
            pl.col("trading_date") == query.trading_date
        )
        if query.sequence is not None:
            before &= pl.col("sequence") == query.sequence
        return self._query(
            "trade_print",
            query.as_of,
            before,
            (pl.col("event_at") >= query.start) & (pl.col("event_at") < query.end),
            dataset=select_trade_dataset(self.datasets, query.dataset),
        )

    def extension_values(self, query: ExtensionQuery):
        dataset = resolve_field(self.datasets, query, query.instrument_ids)
        subjects = (
            pl.col("instrument_id").is_in(query.instrument_ids)
            if query.instrument_ids
            else pl.col("instrument_id").is_null()
        )
        return self._query(
            "extension_value",
            query.as_of,
            subjects & (pl.col("effective_at") <= query.effective_at),
            pl.col("valid_until").is_null() | (pl.col("valid_until") > query.effective_at),
            dataset=dataset,
        )

    def iter_minute_signal_values(self, reference, *, instrument_ids, start, end, as_of):
        """Read historical Boolean edges in one scan, with each bar's own visibility cutoff."""
        dataset = resolve_field(self.datasets, reference, instrument_ids)
        scope = dataset.contents.scope
        field = scope.extension_field
        if (field.value_type, field.granularity, field.time_basis) != ("bool", "instrument", "minute_bar_end"):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Minute signals need instrument Boolean bar-end fields")
        if dataset.contents.knowledge_basis != "historical_versions" or not scope.start <= start < end <= scope.end:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Minute signal field needs historical availability for the requested bar ends",
            )
        return self._iter_query(
            "extension_value",
            as_of,
            pl.col("instrument_id").is_in(instrument_ids)
            & (pl.col("effective_at") >= start)
            & (pl.col("effective_at") < end)
            & (pl.col("available_at") <= pl.col("effective_at")),
            pl.lit(True),
            dataset=dataset,
            keep_effective_history=True,
        )

    def instruments(self, query: HistoryQuery):
        result = self._query(
            "instrument",
            query.as_of,
            pl.col("instrument_id").is_in(query.instrument_ids),
            (pl.col("valid_from") <= query.effective_at)
            & (pl.col("valid_to").is_null() | (pl.col("valid_to") > query.effective_at)),
        )
        if len({record.instrument_id for record in result}) != len(result):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Overlapping active instrument specifications"
            )
        return result

    def _events(self, kind, query):
        return self._query(
            kind,
            query.as_of,
            pl.col("instrument_id").is_in(query.instrument_ids),
            (pl.col("effective_at") <= query.effective_at) & ~pl.col("cancelled"),
        )

    def actions(self, query: HistoryQuery):
        return self._events("corporate_action", query)

    def factors(self, query: HistoryQuery):
        return self._events("adjustment_factor", query)

    def resolve_symbol(self, symbol, *, source_id, venue_id, effective_at, as_of):
        effective_at = utc_datetime(effective_at)
        result = self._query(
            "symbol_mapping",
            utc_datetime(as_of),
            pl.col("source_id") == source_id,
            (pl.col("symbol") == symbol)
            & (pl.col("venue_id") == venue_id)
            & (pl.col("valid_from") <= effective_at)
            & (pl.col("valid_to").is_null() | (pl.col("valid_to") > effective_at)),
        )
        if not result:
            raise TiveloError(ErrorCode.INSTRUMENT_NOT_FOUND, "No visible symbol mapping")
        if len(result) != 1:
            raise TiveloError(ErrorCode.AMBIGUOUS_INSTRUMENT, "Symbol maps to multiple instruments")
        return result[0].instrument_id
