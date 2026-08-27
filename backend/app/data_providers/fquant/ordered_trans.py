"""Offline ordered-trans materializer and immutable published reader."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import io
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import polars as pl

from app.data_providers.schemas import ORDERED_TRANS_MINUTE_COLUMNS

ORDERED_TRANS_ROOT_ENV = "FQUANT_SNAPSHOT_ROOT_ENGINE_A_ORDERED_TRANS"
DEFAULT_ORDERED_TRANS_ROOT = "/Volumes/WD1/duckdb/snapshots/engine-a-ordered-trans"
ORDERED_TRANS_LOGICAL = "tdx_ordered_trans"
ORDERED_TRANS_DATASET = "ordered_trans_minute_v1"
PARSER_VERSION = "tdx_trans_csv_v1"
MANIFEST_SCHEMA_VERSION = 1
SOURCE_SEQUENCE_RULE = "physical_data_row_zero_based"
CURRENT_FILENAME = "current.json"
LOCK_FILENAME = ".publish.lock"
HEADER_SIX = ("time", "price", "vol", "num", "amount", "buyorsell")
HEADER_SEVEN_VENUE = HEADER_SIX + ("venue",)
PARSER_VARIANT_SIX = "six_column"
PARSER_VARIANT_SEVEN_VENUE = "seven_column_venue"
_HEADERS = {HEADER_SIX: PARSER_VARIANT_SIX, HEADER_SEVEN_VENUE: PARSER_VARIANT_SEVEN_VENUE}
_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_GENERATION_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{16}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ARTIFACT_RE = re.compile(r"^bars/date=(\d{4})-(\d{2})-(\d{2})/(\d{6}\.(?:SH|SZ|BJ))\.parquet$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_RAW_CONTINUOUS_MINUTES = tuple(range(570, 690)) + tuple(range(780, 900))
_RAW_BOUNDARY_MINUTES = (690, 900)
_ACCEPTED_RAW_MINUTES = _RAW_CONTINUOUS_MINUTES + _RAW_BOUNDARY_MINUTES
_CLOSE_MINUTES = tuple(x + 1 for x in _RAW_CONTINUOUS_MINUTES)
_CLOSE_MINUTE_INDEX = {minute: index for index, minute in enumerate(_CLOSE_MINUTES)}
if len(_CLOSE_MINUTES) != 240:  # pragma: no cover
    raise RuntimeError("ordered trans session must contain 240 canonical close timestamps")


class OrderedTransIntegrityError(RuntimeError):
    """Any source, manifest, path, parquet or OHLCV integrity failure."""


class MaterializationSkipped(ValueError):
    """A symbol/day is incomplete and therefore excluded from generation."""


class PublishConflict(RuntimeError):
    """Expected-current or generation-name CAS conflict."""


@dataclass(frozen=True, slots=True)
class OrderedTransMinuteBar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class OrderedTransSessionSpec:
    symbol: str
    day: date
    open_time: time
    close_time: time


@dataclass(frozen=True, slots=True)
class Tick:
    raw_minute: int
    price: float
    volume: int
    source_seq: int


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    sha256: str
    size_bytes: int
    header: tuple[str, ...]
    parser_variant: str
    source_rows: int
    ticks: tuple[Tick, ...]


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    symbol: str
    day: date
    source: Mapping[str, Any]
    artifact: Mapping[str, Any]
    parquet_bytes: bytes

    def to_json(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "day": self.day.isoformat(), "source": dict(self.source), "artifact": dict(self.artifact)}


@dataclass(frozen=True, slots=True)
class BuiltGeneration:
    staging_dir: str
    generation: str
    manifest_bytes: bytes
    entries: tuple[ManifestEntry, ...]
    skipped: tuple[tuple[str, str, str], ...]

    @property
    def complete_days(self) -> list[date]:
        return sorted({entry.day for entry in self.entries})


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    status: str
    generation: str
    staging_dir: str
    reason: str | None = None


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_fd(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        part = os.read(fd, 1 << 20)
        if not part:
            break
        chunks.append(part)
        total += len(part)
    if total != size:
        raise OrderedTransIntegrityError(f"single-FD size drift: stat={size}, read={total}")
    return b"".join(chunks)


def read_source_csv_bytes(path: str | os.PathLike[str]) -> bytes:
    target = os.path.abspath(os.fspath(path))
    if not os.path.isfile(target) or os.path.islink(target):
        raise OrderedTransIntegrityError(f"raw source must be a regular non-symlink file: {path}")
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OrderedTransIntegrityError(f"raw source is not regular: {path}")
        payload = _read_fd(fd, info.st_size)
    finally:
        os.close(fd)
    return payload


def _read_relative_nofollow(root: str, relative: str) -> bytes:
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OrderedTransIntegrityError(f"path escape rejected: {relative!r}")
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            os.close(fd)
            fd = nxt
        child = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
    finally:
        os.close(fd)
    try:
        info = os.fstat(child)
        if not stat.S_ISREG(info.st_mode):
            raise OrderedTransIntegrityError(f"artifact is not regular: {relative}")
        return _read_fd(child, info.st_size)
    finally:
        os.close(child)


def _close_timestamps(day: date) -> list[datetime]:
    base = datetime.combine(day, time(0, 0))
    return [base + timedelta(minutes=m) for m in _CLOSE_MINUTES]


def snapshot_source(path: str | os.PathLike[str]) -> SourceSnapshot:
    payload = read_source_csv_bytes(path)
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise OrderedTransIntegrityError(f"source CSV is not UTF-8: {exc}") from exc
    if not lines:
        raise OrderedTransIntegrityError("source CSV has no header")
    header = tuple(field.strip() for field in lines[0].split(","))
    variant = _HEADERS.get(header)
    if variant is None:
        raise OrderedTransIntegrityError(f"unknown source header: {header!r}")
    ticks: list[Tick] = []
    source_rows = 0
    wanted = set(_ACCEPTED_RAW_MINUTES)
    for source_seq, line in enumerate(lines[1:]):
        if not line.strip():
            continue
        fields = line.split(",")
        if len(fields) != len(header):
            raise OrderedTransIntegrityError(f"CSV row width {len(fields)} != {len(header)}")
        source_rows += 1
        match = _TIME_RE.match(fields[0].strip())
        if match is None:
            raise OrderedTransIntegrityError(f"invalid time field: {fields[0]!r}")
        minute = int(match.group(1)) * 60 + int(match.group(2))
        if minute not in wanted:
            continue
        try:
            price = float(fields[1].strip())
        except ValueError as exc:
            raise OrderedTransIntegrityError(f"invalid price: {fields[1]!r}") from exc
        if not math.isfinite(price):
            raise OrderedTransIntegrityError("non-finite price")
        volume_text = fields[2].strip()
        if not volume_text.isdigit():
            raise OrderedTransIntegrityError(f"invalid volume: {volume_text!r}")
        volume = int(volume_text)
        if price > 0 and volume > 0:
            ticks.append(Tick(minute, price, volume, source_seq))
    return SourceSnapshot(sha256_hex(payload), len(payload), header, variant, source_rows, tuple(ticks))


def aggregate_bars(symbol: str, day: date, ticks: Sequence[Tick]) -> tuple[OrderedTransMinuteBar, ...]:
    buckets: dict[int, list[float]] = {}
    for tick in ticks:
        close_minute = (
            tick.raw_minute
            if tick.raw_minute in _RAW_BOUNDARY_MINUTES
            else tick.raw_minute + 1
        )
        current = buckets.get(close_minute)
        if current is None:
            buckets[close_minute] = [tick.price, tick.price, tick.price, tick.price, float(tick.volume)]
        else:
            current[1] = max(current[1], tick.price)
            current[2] = min(current[2], tick.price)
            current[3] = tick.price
            current[4] += tick.volume
    present = sorted(minute for minute in buckets if minute in _CLOSE_MINUTE_INDEX)
    window_ids = {_CLOSE_MINUTE_INDEX[minute] // 5 for minute in present}
    if not present or present[0] != _CLOSE_MINUTES[0] or present[-1] != _CLOSE_MINUTES[-1]:
        raise MaterializationSkipped(f"missing opening/closing trade bar for {symbol} {day}")
    if window_ids != set(range(48)):
        raise MaterializationSkipped(
            f"missing {48 - len(window_ids)} five-minute trade windows for {symbol} {day}"
        )
    base = datetime.combine(day, time(0, 0))
    return tuple(
        OrderedTransMinuteBar(symbol, base + timedelta(minutes=minute), *buckets[minute])
        for minute in present
    )


def _bars_frame(bars: Sequence[OrderedTransMinuteBar]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [bar.symbol for bar in bars], "ts": [bar.ts for bar in bars],
        "open": [bar.open for bar in bars], "high": [bar.high for bar in bars],
        "low": [bar.low for bar in bars], "close": [bar.close for bar in bars],
        "volume": [bar.volume for bar in bars],
    }, schema={"symbol": pl.Utf8, "ts": pl.Datetime("us"), "open": pl.Float64,
               "high": pl.Float64, "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64})


def bars_to_parquet_bytes(bars: Sequence[OrderedTransMinuteBar]) -> bytes:
    if not bars:
        raise OrderedTransIntegrityError("attempted to encode empty bar set")
    frame = _bars_frame(bars)
    verify_bar_frame(frame, bars[0].symbol, bars[0].ts.date())
    buffer = io.BytesIO()
    frame.write_parquet(buffer, compression="zstd")
    return buffer.getvalue()


def verify_bar_frame(frame: pl.DataFrame, symbol: str, day: date) -> None:
    if frame.columns != ORDERED_TRANS_MINUTE_COLUMNS or not 1 <= frame.height <= 240:
        raise OrderedTransIntegrityError(f"artifact schema/rows invalid for {symbol} {day}")
    dtype = frame.schema["ts"]
    if not isinstance(dtype, pl.Datetime) or dtype.time_zone is not None:
        raise OrderedTransIntegrityError("artifact timestamp dtype invalid")
    actual = [value.replace(microsecond=0) for value in frame["ts"].to_list()]
    expected = _close_timestamps(day)
    expected_index = {value: index for index, value in enumerate(expected)}
    if (
        actual != sorted(set(actual))
        or actual[0] != expected[0]
        or actual[-1] != expected[-1]
        or any(value not in expected_index for value in actual)
        or {expected_index[value] // 5 for value in actual} != set(range(48))
    ):
        raise OrderedTransIntegrityError(f"artifact sparse timestamps/windows invalid for {symbol} {day}")
    if frame["symbol"].unique().to_list() != [symbol]:
        raise OrderedTransIntegrityError("artifact symbol mismatch")
    for index, values in enumerate(zip(*(frame[col].to_list() for col in ("open", "high", "low", "close", "volume")), strict=True)):
        o, h, low, close, volume = values
        if any(not isinstance(value, float) or not math.isfinite(value) for value in values):
            raise OrderedTransIntegrityError(f"non-finite OHLCV row {index}")
        if min(o, h, low, close) <= 0 or volume <= 0 or h < max(o, close) or low > min(o, close) or h < low:
            raise OrderedTransIntegrityError(f"invalid OHLCV row {index}")


def decode_artifact_bytes(payload: bytes, symbol: str, day: date) -> pl.DataFrame:
    try:
        frame = pl.read_parquet(io.BytesIO(payload))
    except Exception as exc:
        raise OrderedTransIntegrityError(f"artifact decode failed: {exc}") from exc
    verify_bar_frame(frame, symbol, day)
    return frame


def default_raw_csv_relative_path(day: date, symbol: str) -> str:
    return f"{day:%Y%m%d}/{symbol[-2:].lower()}{symbol[:6]}.csv"


def materialize_symbol_day(raw_root: str | os.PathLike[str], symbol: str, day: date, *, source_relative_path: str | None = None) -> ManifestEntry:
    symbol = symbol.strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise OrderedTransIntegrityError(f"invalid symbol {symbol!r}")
    relative = source_relative_path or default_raw_csv_relative_path(day, symbol)
    if os.path.islink(os.path.join(os.fspath(raw_root), relative)) or not os.path.isfile(os.path.join(os.fspath(raw_root), relative)):
        raise MaterializationSkipped(f"missing raw csv for {symbol} {day}: {relative}")
    snapshot = snapshot_source(os.path.join(os.fspath(raw_root), relative))
    bars = aggregate_bars(symbol, day, snapshot.ticks)
    payload = bars_to_parquet_bytes(bars)
    missing_closes = [
        f"{minute // 60:02d}:{minute % 60:02d}"
        for minute in _CLOSE_MINUTES
        if minute not in {bar.ts.hour * 60 + bar.ts.minute for bar in bars}
    ]
    return ManifestEntry(symbol, day,
        {"relative_path": relative, "size_bytes": snapshot.size_bytes, "sha256": snapshot.sha256,
         "rows": snapshot.source_rows, "header": list(snapshot.header), "parser_variant": snapshot.parser_variant},
        {"relative_path": f"bars/date={day.isoformat()}/{symbol}.parquet", "size_bytes": len(payload),
         "sha256": sha256_hex(payload), "rows": len(bars), "five_minute_windows": 48,
         "missing_close_timestamps": missing_closes,
         "first_close": f"{bars[0].close:.15g}", "last_close": f"{bars[-1].close:.15g}"}, payload)


def build_generation_staging(*, snapshot_root: str | os.PathLike[str], raw_root: str | os.PathLike[str], symbols: Sequence[str], days: Sequence[date]) -> BuiltGeneration:
    normalized = sorted({symbol.strip().upper() for symbol in symbols})
    if not normalized or any(not _SYMBOL_RE.match(symbol) for symbol in normalized):
        raise ValueError("symbols must be canonical A-share symbols")
    staging = os.path.join(os.fspath(snapshot_root), f".staging-{uuid.uuid4().hex}")
    entries: list[ManifestEntry] = []
    skipped: list[tuple[str, str, str]] = []
    os.makedirs(staging, exist_ok=False)
    for day in sorted(set(days)):
        current: list[ManifestEntry] = []
        misses: list[tuple[str, str]] = []
        for symbol in normalized:
            try:
                entry = materialize_symbol_day(raw_root, symbol, day)
            except MaterializationSkipped as exc:
                misses.append((symbol, str(exc)))
                continue
            current.append(entry)
        if misses:
            skipped.extend((day.isoformat(), symbol, reason) for symbol, reason in misses)
            continue
        for entry in current:
            artifact = os.path.join(staging, *entry.artifact["relative_path"].split("/"))
            os.makedirs(os.path.dirname(artifact), exist_ok=True)
            with open(artifact, "wb") as fh:
                fh.write(entry.parquet_bytes)
        entries.extend(current)
    if not entries:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise MaterializationSkipped("no all-symbol complete date")
    core = {"schema_version": 1, "parser_version": PARSER_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "timezone": "Asia/Shanghai", "route": {"env": ORDERED_TRANS_ROOT_ENV, "logical": ORDERED_TRANS_LOGICAL, "dataset": ORDERED_TRANS_DATASET},
            "source_sequence": SOURCE_SEQUENCE_RULE,
            "coverage": {symbol: sorted(entry.day.isoformat() for entry in entries if entry.symbol == symbol) for symbol in normalized},
            "entries": [entry.to_json() for entry in sorted(entries, key=lambda item: (item.symbol, item.day))]}
    digest = sha256_hex(canonical_json_bytes(core))[:16]
    generation = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{digest}"
    manifest = dict(core)
    manifest["generation"] = generation
    manifest_bytes = canonical_json_bytes(manifest)
    with open(os.path.join(staging, "manifest.json"), "wb") as fh:
        fh.write(manifest_bytes)
    return BuiltGeneration(staging, generation, manifest_bytes, tuple(entries), tuple(skipped))


def read_current_bytes(root: str | os.PathLike[str]) -> bytes | None:
    try:
        return _read_relative_nofollow(os.fspath(root), CURRENT_FILENAME)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OrderedTransIntegrityError("current.json must be a regular non-symlink file") from exc


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_staged_generation(root: str | os.PathLike[str], built: BuiltGeneration, expected_current: bytes | None) -> PublishOutcome:
    root = os.fspath(root)
    os.makedirs(root, exist_ok=True)
    lock_fd = os.open(os.path.join(root, LOCK_FILENAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if read_current_bytes(root) != expected_current:
            return PublishOutcome("conflict", built.generation, built.staging_dir, "expected-current changed")
        final = os.path.join(root, built.generation)
        if os.path.exists(final):
            try:
                existing_bytes = _read_relative_nofollow(
                    root, f"{built.generation}/manifest.json"
                )
            except (OSError, OrderedTransIntegrityError):
                return PublishOutcome("name_conflict", built.generation, built.staging_dir, "same generation manifest is unreadable")
            if existing_bytes != built.manifest_bytes:
                return PublishOutcome("name_conflict", built.generation, built.staging_dir, "same generation has different manifest bytes")
            import shutil
            shutil.rmtree(built.staging_dir, ignore_errors=True)
        else:
            for directory, _, files in os.walk(built.staging_dir):
                for name in files:
                    _fsync_file(os.path.join(directory, name))
            os.replace(built.staging_dir, final)
        for directory, _, files in os.walk(final, topdown=False):
            for name in files:
                _fsync_file(os.path.join(directory, name))
            _fsync_dir(directory)
        temp = os.path.join(root, f".current.json.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        with open(temp, "wb") as fh:
            fh.write(canonical_json_bytes({"generation": built.generation}) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, os.path.join(root, CURRENT_FILENAME))
        _fsync_dir(root)
        return PublishOutcome("published", built.generation, final)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


class PublishedOrderedTransMinuteReader:
    """Pinned published generation reader; never scans or opens raw CSV."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = os.path.realpath(os.fspath(root))
        current = read_current_bytes(self._root)
        if current is None:
            raise OrderedTransIntegrityError("ordered-trans current.json unavailable")
        try:
            pointer = json.loads(current)
            generation = pointer["generation"]
        except (ValueError, KeyError, TypeError) as exc:
            raise OrderedTransIntegrityError("invalid current.json") from exc
        if not isinstance(generation, str) or not _GENERATION_RE.match(generation):
            raise OrderedTransIntegrityError("invalid ordered-trans generation")
        manifest_bytes = _read_relative_nofollow(self._root, f"{generation}/manifest.json")
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError as exc:
            raise OrderedTransIntegrityError("invalid manifest JSON") from exc
        if not isinstance(manifest, dict) or canonical_json_bytes(manifest) != manifest_bytes:
            raise OrderedTransIntegrityError("manifest must be canonical JSON")
        if manifest.get("generation") != generation or not isinstance(manifest.get("entries"), list):
            raise OrderedTransIntegrityError("manifest generation mismatch")
        manifest_core = dict(manifest)
        manifest_core.pop("generation")
        if generation.rsplit("-", 1)[-1] != sha256_hex(canonical_json_bytes(manifest_core))[:16]:
            raise OrderedTransIntegrityError("generation content hash mismatch")
        if (
            manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
            or manifest.get("parser_version") != PARSER_VERSION
            or manifest.get("source_sequence") != SOURCE_SEQUENCE_RULE
            or manifest.get("route") != {
                "env": ORDERED_TRANS_ROOT_ENV,
                "logical": ORDERED_TRANS_LOGICAL,
                "dataset": ORDERED_TRANS_DATASET,
            }
        ):
            raise OrderedTransIntegrityError("manifest contract mismatch")
        self._generation = generation
        self._manifest_hash = sha256_hex(manifest_bytes)
        self._entries: dict[tuple[str, date], Mapping[str, Any]] = {}
        for item in manifest["entries"]:
            try:
                symbol = item["symbol"]
                day = date.fromisoformat(item["day"])
                artifact = item["artifact"]
                relative = artifact["relative_path"]
                size_bytes = int(artifact["size_bytes"])
            except (KeyError, TypeError, ValueError) as exc:
                raise OrderedTransIntegrityError("invalid manifest entry") from exc
            match = _ARTIFACT_RE.match(relative)
            if not match or match.group(4) != symbol or match.group(1, 2, 3) != (f"{day:%Y}", f"{day:%m}", f"{day:%d}"):
                raise OrderedTransIntegrityError("artifact path escape or identity mismatch")
            rows = artifact.get("rows")
            if (
                not _HEX64_RE.match(str(artifact.get("sha256", "")))
                or size_bytes < 0
                or not isinstance(rows, int)
                or not 1 <= rows <= 240
                or artifact.get("five_minute_windows") != 48
            ):
                raise OrderedTransIntegrityError("invalid artifact hash/size/rows/windows")
            key = (symbol, day)
            if key in self._entries:
                raise OrderedTransIntegrityError("duplicate manifest symbol/day")
            self._entries[key] = item
        symbols = sorted({symbol for symbol, _ in self._entries})
        if not symbols:
            raise OrderedTransIntegrityError("manifest has no entries")
        day_sets = [{day for (symbol, day) in self._entries if symbol == value} for value in symbols]
        if not day_sets[0] or any(days != day_sets[0] for days in day_sets[1:]):
            raise OrderedTransIntegrityError("manifest coverage is not all-symbol complete")
        computed_coverage = {
            symbol: sorted(day.isoformat() for day in day_sets[index])
            for index, symbol in enumerate(symbols)
        }
        if manifest.get("coverage") != computed_coverage:
            raise OrderedTransIntegrityError("manifest coverage mismatch")
        self._complete_days = sorted(day_sets[0])
        self._catalog_manifest = {
            key: copy.deepcopy(manifest[key])
            for key in (
                "schema_version",
                "parser_version",
                "created_at",
                "timezone",
                "route",
                "source_sequence",
                "coverage",
                "generation",
            )
        }
        self._cache: dict[tuple[str, date], tuple[OrderedTransMinuteBar, ...]] = {}
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise OrderedTransIntegrityError("reader closed")

    def catalog_manifest(self) -> Mapping[str, Any]:
        self._ensure_open()
        return copy.deepcopy(self._catalog_manifest)

    def manifest_sha256(self) -> str:
        self._ensure_open(); return self._manifest_hash

    def generation(self) -> str:
        self._ensure_open(); return self._generation

    def market_days(self, start: date, end: date) -> list[date]:
        self._ensure_open()
        if start > end:
            raise ValueError("start must be <= end")
        return [day for day in self._complete_days if start <= day <= end]

    def session(self, symbol: str, day: date) -> OrderedTransSessionSpec:
        self._ensure_open()
        if (symbol, day) not in self._entries:
            raise OrderedTransIntegrityError("symbol/day outside published coverage")
        return OrderedTransSessionSpec(symbol, day, time(9, 30), time(15, 0))

    def minute_bars(self, symbol: str, day: date) -> tuple[OrderedTransMinuteBar, ...]:
        self._ensure_open()
        key = (symbol, day)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        item = self._entries.get(key)
        if item is None or day not in self._complete_days:
            raise OrderedTransIntegrityError("symbol/day outside complete coverage")
        artifact = item["artifact"]
        payload = _read_relative_nofollow(os.path.join(self._root, self._generation), artifact["relative_path"])
        if len(payload) != artifact["size_bytes"] or sha256_hex(payload) != artifact["sha256"]:
            raise OrderedTransIntegrityError("artifact size/hash mismatch")
        frame = decode_artifact_bytes(payload, symbol, day)
        bars = tuple(OrderedTransMinuteBar(symbol, ts, float(o), float(h), float(low), float(close), float(volume)) for ts, o, h, low, close, volume in zip(frame["ts"].to_list(), frame["open"].to_list(), frame["high"].to_list(), frame["low"].to_list(), frame["close"].to_list(), frame["volume"].to_list(), strict=True))
        self._cache[key] = bars
        return bars

    def sealed_cutoff(self) -> datetime:
        self._ensure_open()
        if not self._complete_days:
            raise OrderedTransIntegrityError("no complete market day")
        return datetime.combine(max(self._complete_days), time(15, 0))

    def close(self) -> None:
        self._cache.clear()
        self._closed = True


__all__ = ["HEADER_SIX", "HEADER_SEVEN_VENUE", "PARSER_VARIANT_SIX", "PARSER_VARIANT_SEVEN_VENUE", "OrderedTransIntegrityError", "MaterializationSkipped", "PublishConflict", "OrderedTransMinuteBar", "OrderedTransSessionSpec", "BuiltGeneration", "PublishOutcome", "snapshot_source", "aggregate_bars", "materialize_symbol_day", "build_generation_staging", "read_current_bytes", "publish_staged_generation", "PublishedOrderedTransMinuteReader", "default_raw_csv_relative_path"]
