"""Read-only, content-pinned import of TSP v1 daily Parquet partitions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal
from pathlib import Path

import polars as pl
import pyarrow as pa
from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from .adapters.cn_daily import CN, RepositoryDailyRequest
from .adapters.cn_observation import CNDailyObservation
from .adapters.v1 import V1BarAdapter, legacy_decimal
from .legacy.daily_validation import CLOSE_CUTOFF, DAILY_REQUIRED, invalid_daily_rows
from .legacy.frame import halted_day
from .legacy.repository_files import DAILY_TABLES as TABLES
from .legacy.repository_files import changed as _changed
from .legacy.repository_files import date_partition_files, verified_parquet
from .legacy.repository_files import identity as _identity
from .legacy.repository_state import (
    EnrichedGenerationUnavailableError,
    assert_usable,
    published_generation,
)
from .providers import ProviderCapability, ProviderDescriptor
from .repository_diagnostics import QuoteReason, RepositoryQuoteIssue, RepositorySourceError
from .repository_precision import row_precision_issues
from .storage import file_digest

# The legacy repository does not identify whether the supplier included post-close trades.
BAR_CONVENTION = "cn.equity.v1.repository.beijing-date"
_OHLC = ("open", "high", "low", "close")
_QFQ_QUANTUM = Decimal("1e-12")


def _quote_failure(ts, day, observed_at) -> QuoteReason | None:
    if ts.date() != day:
        return "quote_date_mismatch"
    if ts.time() < CLOSE_CUTOFF:
        return "quote_before_finalization"
    if ts > observed_at:
        return "quote_after_observation"
    return None


class RepositoryDailyProvider(CNDailyObservation):
    def __init__(self, root: Path, source: RepositoryDailyRequest):
        super().__init__(source, lambda: datetime.now(UTC), days_per_page=1, symbols_per_page=10000)
        self.root = Path(root).resolve(strict=True)
        self.table = self.root / TABLES[source.layout]
        if not self.table.is_dir() or self.table.is_symlink():
            raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "Legacy source table is unavailable")
        if source.layout == "daily_raw_v1" and source.request.price_basis != "raw":
            raise TiveloError(
                ErrorCode.FACTOR_INCOMPATIBLE, "Raw repository only provides raw prices"
            )
        self.start = source.request.start.astimezone(CN).date()
        self.end = (source.request.end - timedelta(microseconds=1)).astimezone(CN).date()
        self.generation = self._check_health()
        self.files = self._inventory()
        self.digests = {}
        self.by_day = {}
        for path in sorted(self.files):
            self.digests[path] = file_digest(path)
            day = date.fromisoformat(path.parent.name.removeprefix("date="))
            self.by_day.setdefault(day, []).append(path)
        self._check_lock()
        declaration = {
            "files": {
                path.relative_to(self.root).as_posix(): digest
                for path, digest in self.digests.items()
            },
            "generation": self.generation,
            "layout": source.layout,
            "bindings": [s.model_dump() for s in source.symbols],
            "preset": self.preset.version_id if self.preset is not None else None,
        }
        self.configuration_digest = hashlib.sha256(
            json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _check_health(self):
        try:
            assert_usable(
                self.root,
                "stock",
                symbols=[s.symbol for s in self.source.symbols],
                end=self.end,
                enriched_only=self.source.layout == "enriched_v1",
            )
            if self.source.layout == "enriched_v1":
                return published_generation(self.root)
        except EnrichedGenerationUnavailableError:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Legacy daily source requires repair or is being published"
            ) from None
        return None

    def _inventory(self):
        return date_partition_files(self.table, self.start, self.end, file_id=_identity)

    def _check_lock(self):
        if self._check_health() != self.generation or self._inventory() != self.files:
            raise _changed()

    def describe(self):
        normalization = self.source.layout
        if self.source.request.price_basis == "qfq":
            normalization += ".qfq-dec12"
        return ProviderDescriptor(
            provider_id="tivelo.v1.repository.daily",
            version="1",
            source_ids=(self.source.expected_provider,),
            capabilities=(
                ProviderCapability(
                    market_id="cn.equity",
                    asset_class="equity",
                    record_type="bar",
                    timeframes=("1d",),
                    price_bases=(self.source.request.price_basis,),
                    knowledge_basis="latest_reconstruction",
                    normalization_version=normalization,
                    bar_convention_id=BAR_CONVENTION,
                ),
            ),
        )

    def check_ready(self, context):
        context.check_cancelled()
        self._check_lock()
        super().check_ready(context)
        self.stamp = self._now()

    def fetch(self, request, cursor, context):
        page = super().fetch(request, cursor, context)
        if page.next_cursor is None:
            self._check_lock()
        return page

    def _rows(self, path, day, bindings, context):
        required = list(DAILY_REQUIRED)
        raw_from_enriched = (
            self.source.layout == "enriched_v1" and self.source.request.price_basis == "raw"
        )
        # A restored mtime is possible on older filesystems. Decode exactly the bytes
        # we hashed, even if an external writer edits its open file in place.
        with verified_parquet(path, self.digests[path], context) as parquet:
            names = parquet.schema_arrow.names
            if not set(required).issubset(names):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Missing legacy daily fields")
            if raw_from_enriched:
                original = ["raw_" + k for k in _OHLC]
                if not set(original).issubset(names):
                    raise TiveloError(
                        ErrorCode.RAW_PRICE_REQUIRED, "Repository has no complete original OHLC"
                    )
                required.extend(original)
            columns = required + [k for k in ("amount", "quote_ts") if k in names]
            if any(pa.types.is_boolean(parquet.schema_arrow.field(k).type) for k in columns):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Boolean field in daily partition")
            offset = 0
            for batch in parquet.iter_batches(columns=columns, batch_size=2048):
                context.check_cancelled()
                frame = pl.from_arrow(batch).with_columns(
                    pl.int_range(offset + 1, offset + 1 + batch.num_rows, dtype=pl.UInt64).alias(
                        "_source_row"
                    )
                )
                offset += batch.num_rows
                frame = frame.filter(pl.col("symbol").is_in(list(bindings)))
                if not frame.is_empty():
                    if not invalid_daily_rows(frame, day).is_empty():
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID, "Invalid legacy daily observation"
                        )
                    yield from frame.with_columns(
                        (~halted_day(frame)).fill_null(False).alias("_traded")
                    ).iter_rows(named=True)

    def _bounds(self, day):
        return self._date_bounds(day.trading_date)

    @staticmethod
    def _date_bounds(day):
        start = datetime.combine(day, time.min, tzinfo=CN)
        return start, start + timedelta(days=1), BAR_CONVENTION

    def _pages(self, context):
        if self.preset is not None:
            for days, symbols in super()._pages(context):
                yield tuple(day.trading_date for day in days), symbols
            return

        request = self.source.request
        observed = self._now()
        for day in sorted(self.by_day):
            context.check_cancelled()
            start, end, _ = self._date_bounds(day)
            if start < request.start or end > request.end:
                raise TiveloError(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Daily import requires whole date buckets"
                )
            if end > observed:
                raise TiveloError(
                    ErrorCode.FUTURE_INFORMATION, "The requested daily bar is unfinished"
                )
            yield (day,), self.source.symbols

    def _read(self, days, symbols, context):
        (day,) = days
        bindings = {s.symbol: s.instrument_id for s in symbols}
        adapter = V1BarAdapter(
            self.source.expected_provider,
            self.source.layout,
            self.source.request.price_basis,
            "1d",
            "CNY",
            "share",
            "lot",
            lambda symbol, _: bindings[symbol],
            self.stamp,
            lot_size=lambda *_: Decimal(100),
            daily_bounds=self._date_bounds,
        )
        records, keys, seen = [], set(), set()
        qfq_context = (
            Context(prec=38, rounding=ROUND_HALF_EVEN) if adapter.price_basis == "qfq" else None
        )
        revision = "observation-" + self.stamp.strftime("%Y%m%dT%H%M%S%fZ")
        try:
            for path in self.by_day.get(day, ()):
                for row in self._rows(path, day, bindings, context):
                    key = (row["symbol"], day)
                    if key in keys:
                        raise TiveloError(
                            ErrorCode.DATA_CONFLICT, "Duplicate repository daily observation"
                        )
                    keys.add(key)
                    if not row["_traded"]:
                        continue
                    stamp = row.get("quote_ts")
                    if stamp is not None:
                        if type(stamp) is not int:
                            raise TiveloError(
                                ErrorCode.CONTRACT_INVALID, "Invalid legacy quote timestamp"
                            )
                        ts = datetime.fromtimestamp(stamp / 1000, CN)
                        reason = _quote_failure(ts, day, self.stamp)
                        if reason:
                            issue = RepositoryQuoteIssue(
                                layout=self.source.layout,
                                trading_date=day,
                                symbol=row["symbol"],
                                row_number=row["_source_row"],
                                file_sha256=self.digests[path],
                                reason=reason,
                            )
                            raise RepositorySourceError((issue,))
                    if qfq_context is not None:
                        # Enriched OHLC was computed in Float64. Publish it at the
                        # same decimal scale used for other derived prices.
                        for column in _OHLC:
                            if isinstance(row[column], float):
                                price = legacy_decimal(row[column])
                                if price.as_tuple().exponent < -12:
                                    row[column] = price.quantize(_QFQ_QUANTUM, context=qfq_context)
                    try:
                        bar = adapter.convert(row, revision_id=revision)
                    except ValidationError as exc:
                        issues = row_precision_issues(
                            exc,
                            self.source.layout,
                            adapter.price_prefix,
                            day,
                            row,
                            self.digests[path],
                        )
                        if issues:
                            raise RepositorySourceError(issues) from None
                        raise
                    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid daily equity price")
                    records.append(bar)
                    seen.add(key)
        except FileNotFoundError:
            raise _changed() from None
        except (
            pa.ArrowException,
            pl.exceptions.PolarsError,
            ValidationError,
            ValueError,
            OverflowError,
            TypeError,
        ):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid legacy daily partition"
            ) from None
        self._record_coverage(bindings, (day,), seen)
        return tuple(records)
