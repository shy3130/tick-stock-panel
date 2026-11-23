"""Retrospective import of v1 mainland stock minute partitions."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb
from tivelo_contracts.data import Coverage, CoverageIssue
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_market_cn.calendar import CNCalendar
from tivelo_market_cn.presets import compile_calendar, load_calendar_preset

from .adapters.cn_daily import CN, RepositoryMinuteRequest
from .adapters.cn_minute import MINUTE, regular_slots
from .adapters.v1 import V1BarAdapter, legacy_decimal
from .legacy.daily_validation import CLOSE_CUTOFF
from .legacy.minute_archive import PRICES, VALUES, inventory, stage_archives
from .legacy.minute_prices import MAX_ANCHOR_RATIO, MIN_ANCHOR_RATIO
from .legacy.repository_files import changed
from .providers import ProviderCapability, ProviderDescriptor
from .repository_pages import PagedRepositoryProvider
from .storage import file_digest


def _source_rows(batches, context):
    for batch in batches:
        context.check_cancelled()
        yield from zip(*(c.to_pylist() for c in batch.columns), strict=True)


def _quotes(rows, slots, terminal):
    # At most one symbol-day is held in memory, even across old per-symbol files.
    selected = {}
    for stamp, candidates in groupby(rows, itemgetter(2)):
        if stamp not in slots and stamp != terminal:
            continue
        remaining = iter(candidates)
        first = next(remaining)
        values = tuple(legacy_decimal(v) if v is not None else None for v in first[4:])
        for other in remaining:
            if other[3] != first[3]:
                break  # Dated partitions take precedence over older symbol partitions in v1.
            if values != tuple(legacy_decimal(v) if v is not None else None for v in other[4:]):
                raise TiveloError(ErrorCode.DATA_CONFLICT, "Conflicting minute observations")
        record = dict(zip(VALUES, values, strict=True))
        if record["volume"] is None or record["volume"] < 0:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid stock minute volume")
        if record["volume"] == 0:
            if record["amount"] not in (None, Decimal(0)):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Turnover reported without volume")
            continue
        if any(record[k] is None or record[k] <= 0 for k in PRICES) or (
            record["low"] > min(record["open"], record["close"])
            or record["high"] < max(record["open"], record["close"])
        ):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid stock minute OHLC")
        selected[stamp] = record
    return selected


def _scale_close(values, raw_close, terminal_close):
    if raw_close == terminal_close:
        return values
    result = dict(values)
    with localcontext(Context(prec=96, rounding=ROUND_HALF_EVEN)):
        for name in PRICES:
            price = (values[name] * raw_close / terminal_close).quantize(Decimal("1e-12"))
            if not price:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Reconstructed minute price underflows"
                )
            result[name] = price
    return result


class RepositoryMinuteProvider(PagedRepositoryProvider):
    def __init__(self, root: Path, source: RepositoryMinuteRequest):
        self.source = source
        self.root = Path(root).resolve(strict=True)
        self.bindings = {s.symbol: s for s in source.symbols}
        self.first = source.request.start.astimezone(CN).date()
        self.last = (source.request.end - timedelta(microseconds=1)).astimezone(CN).date()
        self.preset = load_calendar_preset(version=source.calendar_version)
        self.marker, self.minutes, self.anchors = self._inventory()
        if self.marker is None and source.price_evidence == "marked_raw":
            raise TiveloError(
                ErrorCode.RAW_PRICE_REQUIRED, "Stock minute archive has no raw-basis marker"
            )
        if self.marker is not None and source.price_evidence == "legacy_close_anchor":
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Legacy close reconstruction requires an unmarked archive"
            )
        self.hashes = {path: file_digest(path) for path in (*self.minutes, *self.anchors)}
        self._check_lock()
        self.basis = "marked-raw" if self.marker is not None else "daily-close-dec12"
        self.convention = "cn.equity.v1.repository.regular.1m." + source.minute_label
        declaration = {
            "files": {
                p.relative_to(self.root).as_posix(): digest for p, digest in self.hashes.items()
            },
            "calendar": self.preset.version_id,
            "bindings": [s.model_dump() for s in source.symbols],
            "price_evidence": self.basis,
            "convention": self.convention,
        }
        self.configuration_digest = hashlib.sha256(
            json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _inventory(self):
        return inventory(self.root, self.first, self.last, self.bindings)

    def _check_lock(self):
        try:
            same = self._inventory() == (self.marker, self.minutes, self.anchors)
        except (TiveloError, FileNotFoundError):
            same = False
        if not same:
            raise changed()

    def describe(self):
        return ProviderDescriptor(
            provider_id="tivelo.v1.repository.minute",
            version="1",
            source_ids=(self.source.expected_provider,),
            capabilities=(
                ProviderCapability(
                    market_id="cn.equity",
                    asset_class="equity",
                    record_type="bar",
                    timeframes=("1m",),
                    price_bases=("raw",),
                    knowledge_basis="latest_reconstruction",
                    normalization_version="minute.v1." + self.basis,
                    bar_convention_id=self.convention,
                ),
            ),
        )

    def check_ready(self, context):
        context.check_cancelled()
        self._check_lock()
        self.requested = 0
        self.missing = {}
        self._start_pages(self._stream(context))

    def _daily_closes(self, connection, day):
        closes = {}
        for symbol, value, quote in connection.execute(
            "SELECT symbol, raw_close, quote_ts FROM anchors WHERE day=?", [day]
        ).fetchall():
            if quote is not None:
                stamp = datetime.fromtimestamp(quote / 1000, CN)
                if stamp.date() != day or stamp.time() < CLOSE_CUTOFF or stamp > self.observed:
                    raise TiveloError(
                        ErrorCode.RAW_PRICE_REQUIRED, "Daily close evidence is not final"
                    )
            close = legacy_decimal(value)
            if symbol in closes and closes[symbol] != close:
                raise TiveloError(ErrorCode.DATA_CONFLICT, "Conflicting raw daily close anchors")
            closes[symbol] = close
        return closes

    def _symbol_bars(self, symbol, rows, slots, terminal, closes, adapter):
        quotes = _quotes(rows, slots, terminal if self.marker is None else None)
        present = slots.keys() & quotes.keys()
        if len(present) < len(slots):
            self.missing.setdefault(symbol, next(stamp for stamp in slots if stamp not in present))
        if not present:
            return
        if self.marker is None:
            close_bar = quotes.get(terminal)
            raw_close = closes.get(symbol)
            if close_bar is None or raw_close is None or raw_close <= 0:
                raise TiveloError(
                    ErrorCode.RAW_PRICE_REQUIRED,
                    "Legacy stock minute lacks a final raw close anchor",
                )
            terminal_close = close_bar["close"]
            ratio = float(terminal_close) / float(raw_close)
            if not math.isfinite(ratio) or not MIN_ANCHOR_RATIO < ratio < MAX_ANCHOR_RATIO:
                raise TiveloError(
                    ErrorCode.RAW_PRICE_REQUIRED, "Legacy closing anchor ratio is unusable"
                )
            end = terminal.replace(tzinfo=CN) + (
                MINUTE if self.source.minute_label == "start" else timedelta()
            )
            if end > self.observed:
                raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Legacy close anchor is unfinished")
        for stamp in sorted(present):
            values = quotes[stamp]
            if self.marker is None:
                values = _scale_close(values, raw_close, terminal_close)
            yield adapter.convert(
                {**values, "symbol": symbol, "datetime": stamp},
                revision_id=self.revision,
                minute_session_id=slots[stamp],
            )

    def _market_days(self):
        calendars = {}
        for venue in {s.venue_id for s in self.source.symbols}:
            version = compile_calendar(self.preset, venue_id=venue, start=self.first, end=self.last)
            calendar = CNCalendar(version)
            if not callable(getattr(calendar, "regular_sessions", None)):
                raise TiveloError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    "The installed market package lacks regular minute sessions",
                )
            calendars[venue] = calendar
        return calendars

    def _stream(self, context):
        with TemporaryDirectory(prefix="tivelo-minute-") as temp:
            directory = Path(temp)
            with duckdb.connect(str(directory / "selected.duckdb")) as connection:
                connection.execute("SET threads = 1")
                connection.execute("SET memory_limit = '256MB'")
                connection.execute("SET temp_directory = ?", [str(directory / "sort")])
                stage_archives(connection, directory, self, context)
                self._check_lock()
                self.observed = datetime.now(UTC)
                if self.source.request.end > self.observed:
                    raise TiveloError(
                        ErrorCode.FUTURE_INFORMATION, "Minute capture extends into the future"
                    )
                self.revision = "observation-" + self.observed.strftime("%Y%m%dT%H%M%S%fZ")
                adapter = V1BarAdapter(
                    self.source.expected_provider,
                    "minute_raw_v1",
                    "raw",
                    "1m",
                    "CNY",
                    "share",
                    "lot",
                    lambda symbol, _: self.bindings[symbol].instrument_id,
                    self.observed,
                    lot_size=lambda *_: Decimal(100),
                    minute_label=self.source.minute_label,
                )
                calendars = self._market_days()
                ordered = connection.execute("""SELECT stamp::DATE AS day, symbol, stamp, priority, o, h, l, c, v, a
                                                FROM minutes ORDER BY day, symbol, stamp, priority""")
                with ordered.to_arrow_reader(4096) as batches, connection.cursor() as lookups:
                    groups = groupby(_source_rows(batches, context), itemgetter(0))
                    group = next(groups, None)
                    day = self.first
                    while day <= self.last:
                        context.check_cancelled()
                        slots = {
                            venue: regular_slots(
                                calendar.regular_sessions(day, as_of=self.observed),
                                self.source.request,
                                self.source.minute_label,
                            )
                            for venue, calendar in calendars.items()
                        }
                        symbols_seen = set()
                        if group is not None and group[0] == day:
                            closes = self._daily_closes(lookups, day) if self.marker is None else {}
                            for symbol, rows in groupby(group[1], itemgetter(1)):
                                symbols_seen.add(symbol)
                                expected, terminal = slots[self.bindings[symbol].venue_id]
                                if expected:
                                    yield from self._symbol_bars(
                                        symbol, rows, expected, terminal, closes, adapter
                                    )
                            group = next(groups, None)
                        for symbol, binding in self.bindings.items():
                            expected, _ = slots[binding.venue_id]
                            self.requested += len(expected)
                            if expected and symbol not in symbols_seen:
                                self.missing.setdefault(symbol, next(iter(expected)))
                        day += timedelta(days=1)

    def _coverage(self):
        issues = tuple(
            CoverageIssue(
                instrument_id=self.bindings[symbol].instrument_id,
                reason="missing",
                detail=f"No traded regular minute for the source label {stamp.isoformat()} Beijing time",
            )
            for symbol, stamp in sorted(self.missing.items())
        )
        if not self.requested:
            issues = (
                CoverageIssue(
                    reason="no_trades", detail="No whole regular minute in the requested sessions"
                ),
            )
        return Coverage(
            status="empty" if not self.count else ("partial" if issues else "complete"),
            requested_units=self.requested or len(self.bindings),
            completed_units=self.count,
            row_count=self.count,
            issues=issues,
        )
