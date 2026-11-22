"""Read TSP v1 stock ex-rights ratios as an explicitly retrospective observation."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime, time
from itertools import chain, groupby
from operator import itemgetter
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from tivelo_contracts.data import AdjustmentFactor, Coverage, CoverageIssue
from tivelo_contracts.errors import ErrorCode, TiveloError

from .adapters.cn_daily import CN, RepositoryFactorRequest
from .adapters.cn_factors import DEFINITIONS, factor_values
from .adapters.v1 import legacy_decimal
from .legacy.repository_files import FACTOR_TABLE, changed, copy_verified, identity
from .providers import ProviderCapability, ProviderDescriptor
from .repository_diagnostics import RepositoryFactorIssue, RepositorySourceError
from .repository_pages import PagedRepositoryProvider
from .repository_precision import decimal_precision_reason
from .storage import file_digest


def _has_wide_ratio(schema):
    index = schema.get_field_index("ex_factor")
    if index < 0:
        return False
    ratio_type = schema.field(index).type
    return pa.types.is_decimal(ratio_type) and ratio_type.precision > 38


def _scan_path(path, context):
    schema = pq.read_schema(path)
    tests = {
        "symbol": lambda t: pa.types.is_string(t) or pa.types.is_large_string(t),
        "trade_date": pa.types.is_date,
        "ex_factor": lambda t: (
            pa.types.is_floating(t) or pa.types.is_integer(t) or pa.types.is_decimal(t)
        ),
    }
    for name, accepts in tests.items():
        index = schema.get_field_index(name)
        if index < 0 or not accepts(schema.field(index).type):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid legacy factor columns")
    if not _has_wide_ratio(schema):
        return path

    # DuckDB reads wider Parquet decimals as DOUBLE. Preserve their decimal text
    # and original row order before letting SQL filter and sort these observations.
    target = path.with_name("exact-ratios.parquet")
    projected = pa.schema(
        [schema.field("symbol"), schema.field("trade_date"), ("ex_factor", pa.string())]
    )
    with pq.ParquetFile(path) as source, pq.ParquetWriter(target, projected) as writer:
        for batch in source.iter_batches(
            batch_size=2048, columns=projected.names, use_threads=False
        ):
            context.check_cancelled()
            writer.write_batch(batch.select(projected.names).cast(projected))
    return target


def _earlier_ratio(duplicates):
    evidence = None
    for _, _, value, _ in duplicates:
        try:
            ratio = legacy_decimal(value)
        except TiveloError:
            continue
        if ratio <= 0:
            continue
        if evidence is not None and evidence != ratio:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Conflicting legacy factors on one date")
        evidence = ratio
    return evidence


def _unique_dates(rows, raw_digest, baseline_date):
    for day, duplicates in groupby(rows, itemgetter(1)):
        if day is None:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Legacy factor has no effective date")
        if baseline_date is not None and day < baseline_date:
            # Earlier events merely witness the series; their ratios never enter
            # the fixed-baseline product. Invalid values are not evidence.
            evidence = _earlier_ratio(duplicates)
            if evidence is not None:
                yield day, evidence
            continue
        symbol, _, value, ordinal = next(duplicates)
        ratio = legacy_decimal(value)
        if ratio <= 0:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Legacy factor must be positive")
        if any(legacy_decimal(row[2]) != ratio for row in duplicates):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Conflicting legacy factors on one date")
        if raw_digest is not None:
            reason = decimal_precision_reason(ratio)
            if reason is not None:
                issue = RepositoryFactorIssue(
                    trading_date=day,
                    symbol=symbol,
                    row_number=ordinal,
                    file_sha256=raw_digest,
                    reason=reason,
                )
                raise RepositorySourceError((issue,))
        yield day, ratio


def _rows(batches, context):
    for batch in batches:
        context.check_cancelled()
        yield from zip(*(column.to_pylist() for column in batch.columns), strict=True)


class RepositoryFactorProvider(PagedRepositoryProvider):
    def __init__(self, root: Path, source: RepositoryFactorRequest):
        self.source = source
        self.path = Path(root).resolve(strict=True) / FACTOR_TABLE / "all.parquet"
        self.signature = self._signature()
        self.digest = file_digest(self.path)
        try:
            self.wide_ratio = _has_wide_ratio(pq.read_schema(self.path))
        except FileNotFoundError:
            raise changed() from None
        except pa.ArrowException:
            # Malformed archives are diagnosed when the locked bytes are read.
            self.wide_ratio = False
        self._check_lock()
        self.bindings = {entry.symbol: entry.instrument_id for entry in source.symbols}
        self.start = source.request.start.astimezone(CN).date()
        self.end = source.request.end.astimezone(CN).date()
        self.definition_id = DEFINITIONS[source.representation]
        self.baseline_id = (
            f"v1:{source.expected_provider}:{source.baseline_date or 'event-pre-post'}"
        )
        declaration = {
            "table": FACTOR_TABLE + "/all.parquet",
            "sha256": self.digest,
            "bindings": [s.model_dump() for s in source.symbols],
            "definition": self.definition_id,
            "baseline": self.baseline_id,
        }
        if self.wide_ratio:
            declaration["ratio_decoder"] = "parquet-wide-decimal-exact-v2"
        if source.baseline_date is not None:
            declaration["ignore_invalid_prebaseline"] = True
        self.configuration_digest = hashlib.sha256(
            json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _signature(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "No legacy stock factor file") from None
        if self.path.parent.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid legacy stock factor path")
        return identity(info)

    def _check_lock(self):
        try:
            same = self._signature() == self.signature
        except TiveloError:
            same = False
        if not same:
            raise changed()

    def describe(self):
        return ProviderDescriptor(
            provider_id="tivelo.v1.repository.factors",
            version="2" if self.wide_ratio else "1",
            source_ids=(self.source.expected_provider,),
            capabilities=(
                ProviderCapability(
                    record_type="adjustment_factor",
                    market_id="cn.equity",
                    asset_class="equity",
                    knowledge_basis="latest_reconstruction",
                    normalization_version=self.definition_id,
                ),
            ),
        )

    def _factor(self, instrument, day, value):
        return AdjustmentFactor(
            factor_id=self.source.representation + ":" + day.isoformat(),
            instrument_id=instrument,
            effective_at=datetime.combine(day, time.min, tzinfo=CN),
            available_at=self.stamp,
            value=value,
            definition_id=self.definition_id,
            factor_kind=self.source.representation,
            baseline_id=self.baseline_id,
            source_id=self.source.expected_provider,
            revision_id=self.revision_id,
        )

    def _for_instrument(self, instrument, dates):
        values = factor_values(
            dates,
            representation=self.source.representation,
            start=self.start,
            baseline_date=self.source.baseline_date,
        )
        for day, value in values:
            yield self._factor(instrument, day, value)

    def _stream(self, context):
        with TemporaryDirectory(prefix="tivelo-factors-") as directory:
            frozen = Path(directory) / "factors.parquet"
            try:
                with frozen.open("wb") as output:
                    copy_verified(self.path, output, self.digest, context)
            except FileNotFoundError:
                raise changed() from None
            self._check_lock()
            # The earliest observation backed by these locked bytes is the capture time.
            self.stamp = datetime.now(UTC)
            self.revision_id = "observation-" + self.stamp.strftime("%Y%m%dT%H%M%S%fZ")
            if self.source.request.end > self.stamp:
                raise TiveloError(
                    ErrorCode.FUTURE_INFORMATION, "Factor capture extends into the future"
                )
            scan = _scan_path(frozen, context)
            with duckdb.connect(":memory:") as connection:
                connection.execute("SET threads = 1")
                connection.execute("SET preserve_insertion_order = true")
                connection.execute("SET memory_limit = '256MB'")
                connection.execute("SET temp_directory = ?", [str(Path(directory) / "sort")])
                raw_digest = self.digest if self.source.representation == "event_ratio" else None
                lower = self.start if raw_digest is not None else None
                # Number the physical input before filtering or sorting. Cumulative views
                # only need event dates and can still push their predicates into the file scan.
                ordinal = "row_number() OVER ()" if raw_digest is not None else "NULL::BIGINT"
                query = connection.execute(
                    f"""SELECT symbol, trade_date, ex_factor, ordinal FROM (
                         SELECT symbol, trade_date, ex_factor, {ordinal} AS ordinal
                           FROM read_parquet(?)
                       ) AS source
                       WHERE symbol IN (SELECT unnest(?))
                         AND (trade_date IS NULL OR
                              (trade_date < ? AND (?::DATE IS NULL OR trade_date >= ?)))
                       ORDER BY symbol, trade_date, ordinal""",
                    [str(scan), list(self.bindings), self.end, lower, lower],
                )
                with query.to_arrow_reader(2048) as batches:
                    for symbol, group in groupby(_rows(batches, context), itemgetter(0)):
                        instrument = self.bindings[symbol]
                        dates = _unique_dates(group, raw_digest, self.source.baseline_date)
                        first = next(dates, None)
                        if first is None:
                            continue
                        yield from self._for_instrument(instrument, chain((first,), dates))
                        self.seen.add(instrument)

    def check_ready(self, context):
        context.check_cancelled()
        self._check_lock()
        self._start_pages(self._stream(context))
        self.seen = set()

    def _coverage(self):
        request = self.source.request
        issues = tuple(
            CoverageIssue(
                instrument_id=key,
                reason="missing",
                detail="No supporting stock factor in the selected archive interval",
            )
            for key in sorted(set(request.instrument_ids) - self.seen)
        )
        return Coverage(
            status="empty" if not self.count else ("partial" if issues else "complete"),
            requested_units=len(request.instrument_ids),
            completed_units=len(self.seen),
            row_count=self.count,
            issues=issues,
        )
