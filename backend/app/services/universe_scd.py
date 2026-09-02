"""Forward eligible-universe SCD — immutable published generations, next-market-day effectiveness.

On-disk layout under the dedicated root (``TICKFLOW_UNIVERSE_SCD_ROOT``):

    <root>/current.json                 -> {"generation": "<UTCts>-<digest16>"}
    <root>/.publish.lock                -> flock for publisher mutual exclusion
    <root>/<generation>/manifest.json   -> canonical JSON manifest (ledger included)
    <root>/<generation>/symbols.json    -> canonical JSON sorted unique A-share symbols

Hard boundaries (docs/ISSUE-14/production-plan.md):

- The PIT universe exists only from the first REAL collection's **next market
  day**. Every earlier ``event_date`` is unavailable; nothing is backfilled
  from current instruments, bars, ``ssdate`` or any history table.
- The collector pins the exact published fstore generation: it validates
  ``current.json`` and the exact generation's ``manifest.json`` plus its
  ``logical="fstore"`` entry, rejects raw fallback / symlinks / path escape,
  and queries through one read-only connection on that exact file. Provider
  caches and current resolvers are never consulted for the universe query.
- eligible v1: canonical A-share symbol with a parseable
  ``ssdate <= collection_date``. The pinned schema carries no reliable
  delisting/suspension status, so the manifest records
  ``status_filter="unavailable"`` instead of claiming such a filter.
- ``available_at`` is fixed as a UTC timestamp; ``effective_from`` is the
  next market day AFTER the collection date, so a post-close collection never
  covers its own day. The previous open interval closes on the market day
  before the new ``effective_from``. Missing collection days create no
  snapshots; a known set simply remains in force until the next real
  ``effective_from``.
- Publishing is parent expected-current CAS + flock + fsync + atomic replace
  into an immutable generation directory. Same-day republication with the
  same source generation and content is idempotent; same-day different
  content is a conflict that leaves ``current.json`` untouched.

Any manifest/hash/path/interval/read fault is a fail-closed integrity error:
callers observe a whole-run unavailable, never a partial or fabricated set.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import logging

logger = logging.getLogger(__name__)

UNIVERSE_SCD_ROOT_ENV = "TICKFLOW_UNIVERSE_SCD_ROOT"
DEFAULT_UNIVERSE_SCD_ROOT = "/Volumes/WD1/duckdb/snapshots/tickflow-universe-scd"
CURRENT_FILENAME = "current.json"
LOCK_FILENAME = ".publish.lock"
MANIFEST_SCHEMA_VERSION = 1
RULE_VERSION = "eligible_v1"
ARTIFACT_NAME = "universe_scd"
STATUS_FILTER = "unavailable"

FSTORE_ROOT_ENV = "FQUANT_SNAPSHOT_ROOT_FSTORE"
FSTORE_ROOT_DEFAULT = "/Volumes/WD1/duckdb/snapshots/fstore"
FSTORE_LOGICAL = "fstore"


_CST = timezone(timedelta(hours=8))
_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_GENERATION_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{16}$")
_FSTORE_GENERATION_RE = re.compile(r"^\d{8}T\d{6}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class UniverseScdIntegrityError(RuntimeError):
    """Any source, manifest, path, hash, interval or read integrity failure."""


class UniverseScdNotPublished(UniverseScdIntegrityError):
    """The root has no published generation yet (before the first real collection)."""


class UniverseScdNoCoverage(UniverseScdIntegrityError):
    """No interval covers the requested event date (notably: before the first effective day)."""


class UniverseScdConflict(UniverseScdIntegrityError):
    """Same-day republication with different source/content, or a non-appendable ledger state."""


# ---------------------------------------------------------------------------
# Canonical JSON / hashing / path-guarded reads
# ---------------------------------------------------------------------------


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
        raise UniverseScdIntegrityError(f"single-FD size drift: stat={size}, read={total}")
    return b"".join(chunks)


def _read_relative_nofollow(root: str, relative: str) -> bytes:
    """Read ``relative`` under ``root`` without following any symlink component."""
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UniverseScdIntegrityError(f"path escape rejected: {relative!r}")
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
            raise UniverseScdIntegrityError(f"artifact is not a regular file: {relative}")
        return _read_fd(child, info.st_size)
    finally:
        os.close(child)


def _read_current(root: str) -> bytes | None:
    try:
        return _read_relative_nofollow(root, CURRENT_FILENAME)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UniverseScdIntegrityError("current.json must be a regular non-symlink file") from exc


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


def _require_json(payload: bytes, what: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise UniverseScdIntegrityError(f"{what} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise UniverseScdIntegrityError(f"{what} must be a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# Root resolution and data-dir guard
# ---------------------------------------------------------------------------


def universe_scd_root() -> str:
    return os.environ.get(UNIVERSE_SCD_ROOT_ENV) or DEFAULT_UNIVERSE_SCD_ROOT


def validate_root_outside_data_dir(root: str | os.PathLike[str], data_dir: str | os.PathLike[str]) -> None:
    """Reject a root that equals or lives inside the user data directory."""
    resolved_root = Path(root).resolve()
    resolved_data = Path(data_dir).resolve()
    if resolved_root == resolved_data or resolved_data in resolved_root.parents:
        raise UniverseScdIntegrityError(
            f"universe SCD root {resolved_root} must live outside the data directory {resolved_data}"
        )


# ---------------------------------------------------------------------------
# Collector: pin the exact published fstore generation, freeze eligible v1
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectionDraft:
    available_at: str
    collection_date: date
    effective_from: date
    prev_market_day: date | None
    calendar_identity: str
    calendar_contract: str
    source: dict[str, Any]
    symbols: tuple[str, ...]
    content_hash: str


def fstore_snapshot_root() -> str:
    return os.environ.get(FSTORE_ROOT_ENV) or FSTORE_ROOT_DEFAULT


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _pin_fstore_source() -> tuple[dict[str, Any], str]:
    """Validate and pin the exact published fstore generation file.

    Returns the manifest-embedded source identity and the absolute file path
    of the pinned ``logical="fstore"`` artifact. Never falls back to raw.
    """
    root = fstore_snapshot_root()
    try:
        pointer_bytes = _read_relative_nofollow(root, CURRENT_FILENAME)
    except FileNotFoundError:
        raise UniverseScdNotPublished(f"fstore snapshot is not published: {root}") from None
    except OSError as exc:
        raise UniverseScdIntegrityError("fstore current.json must be a regular non-symlink file") from exc
    pointer = _require_json(pointer_bytes, "fstore current.json")
    generation = pointer.get("generation")
    if not isinstance(generation, str) or not _FSTORE_GENERATION_RE.match(generation):
        raise UniverseScdIntegrityError(f"invalid fstore generation pointer: {generation!r}")
    try:
        manifest_bytes = _read_relative_nofollow(root, f"{generation}/manifest.json")
    except FileNotFoundError as exc:
        raise UniverseScdIntegrityError(f"fstore generation manifest is missing: {generation}") from exc
    manifest = _require_json(manifest_bytes, "fstore generation manifest")
    if manifest.get("generation") != generation:
        raise UniverseScdIntegrityError("fstore manifest generation does not match current.json")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise UniverseScdIntegrityError("fstore manifest entries must be a list")
    entry = next((item for item in entries if isinstance(item, dict) and item.get("logical") == FSTORE_LOGICAL), None)
    if entry is None:
        raise UniverseScdIntegrityError(f"fstore manifest has no logical={FSTORE_LOGICAL!r} entry")
    file_name = entry.get("file")
    if not isinstance(file_name, str) or not file_name or "/" in file_name or file_name in (".", ".."):
        raise UniverseScdIntegrityError(f"fstore manifest file identity rejected: {file_name!r}")
    generation_dir = os.path.join(root, generation)
    if os.path.islink(generation_dir) or not os.path.isdir(generation_dir):
        raise UniverseScdIntegrityError(f"fstore generation must be a real directory: {generation}")
    file_path = os.path.join(generation_dir, file_name)
    info = os.lstat(file_path) if not os.path.islink(file_path) else None
    if info is None or not stat.S_ISREG(info.st_mode):
        raise UniverseScdIntegrityError(f"pinned fstore artifact must be a regular non-symlink file: {file_name}")
    declared_size = entry.get("size_bytes")
    if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size < 0:
        raise UniverseScdIntegrityError("fstore manifest size_bytes must be a nonnegative integer")
    if declared_size != info.st_size:
        raise UniverseScdIntegrityError(
            f"pinned fstore artifact size drift: manifest={declared_size}, actual={info.st_size}"
        )
    source = {
        "artifact": "fstore_snapshot",
        "root_env": FSTORE_ROOT_ENV,
        "root": root,
        "logical": FSTORE_LOGICAL,
        "generation": generation,
        "manifest_sha256": sha256_hex(manifest_bytes),
        "file": file_name,
        "size_bytes": info.st_size,
    }
    return source, file_path

def _eligible_symbols_from_fstore(conn: Any, collection_date: date) -> tuple[str, ...]:
    """Query eligible v1 from the already pinned read-only fstore connection."""
    from app.data_providers.fquant.symbols import code_to_symbol

    rows = conn.execute("SELECT code, ssdate FROM base_infos WHERE asset_type = 1").fetchall()
    symbols: set[str] = set()
    for code, ssdate in rows:
        if code is None:
            continue
        symbol = code_to_symbol(str(code), 1)
        if not _SYMBOL_RE.match(symbol):
            continue
        listing = _coerce_date(ssdate)
        if listing is None or listing > collection_date:
            continue
        symbols.add(symbol)
    if not symbols:
        raise UniverseScdIntegrityError("pinned fstore base_infos produced no eligible a-share symbols")
    return tuple(sorted(symbols))


def _fstore_market_days(conn: Any, collection_date: date) -> tuple[date, date | None]:
    """Resolve next/previous sessions from the exact pinned fstore trade_date table."""
    rows = conn.execute(
        "SELECT tdate FROM trade_date WHERE tdate > ? AND isopen = 3 ORDER BY tdate LIMIT 1",
        [collection_date.isoformat()],
    ).fetchall()
    if not rows:
        raise UniverseScdIntegrityError("pinned fstore trade_date has no next market day")
    next_day = _coerce_date(rows[0][0])
    if next_day is None:
        raise UniverseScdIntegrityError("pinned fstore trade_date next tdate is invalid")
    previous_rows = conn.execute(
        "SELECT tdate FROM trade_date WHERE tdate < ? AND isopen = 3 ORDER BY tdate DESC LIMIT 1",
        [next_day.isoformat()],
    ).fetchall()
    previous = _coerce_date(previous_rows[0][0]) if previous_rows else None
    return next_day, previous


def collect_eligible_universe(*, now: datetime) -> CollectionDraft:
    """Pin one exact fstore file and read trade_date + base_infos through one connection."""
    collection_date = now.astimezone(_CST).date()
    available_at = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    source, file_path = _pin_fstore_source()
    from app.storage.duckdb_runtime import connect_duckdb

    conn = connect_duckdb(file_path, read_only=True)
    try:
        effective_from, prev_market_day = _fstore_market_days(conn, collection_date)
        symbols = _eligible_symbols_from_fstore(conn, collection_date)
    finally:
        conn.close()
    calendar_contract = "fstore_trade_date:tdate,isopen,mkt,lastdate,nextdate"
    calendar_identity = f"fstore_trade_date:{source['generation']}:{source['manifest_sha256']}"
    return CollectionDraft(
        available_at=available_at,
        collection_date=collection_date,
        effective_from=effective_from,
        prev_market_day=prev_market_day,
        calendar_identity=calendar_identity,
        calendar_contract=calendar_contract,
        source=source,
        symbols=symbols,
        content_hash=sha256_hex(canonical_json_bytes(list(symbols))),
    )


# ---------------------------------------------------------------------------
# Interval ledger
# ---------------------------------------------------------------------------


def _require_iso_date(value: Any, what: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise UniverseScdIntegrityError(f"{what} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UniverseScdIntegrityError(f"{what} is not an ISO date: {value!r}") from exc


def _parse_intervals(raw: Any) -> list[dict[str, Any]]:
    """Validate the interval ledger: ordered, non-overlapping, at most one open (last)."""
    if not isinstance(raw, list) or not raw:
        raise UniverseScdIntegrityError("manifest intervals must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise UniverseScdIntegrityError("interval entries must be JSON objects")
        effective_from = _require_iso_date(entry.get("effective_from"), "interval effective_from")
        effective_to_raw = entry.get("effective_to")
        effective_to = None if effective_to_raw is None else _require_iso_date(effective_to_raw, "interval effective_to")
        content_hash = entry.get("content_hash")
        if not isinstance(content_hash, str) or not _HEX64_RE.match(content_hash):
            raise UniverseScdIntegrityError("interval content_hash must be a sha256 hex digest")
        available_at = entry.get("available_at")
        if not isinstance(available_at, str) or not available_at:
            raise UniverseScdIntegrityError("interval available_at must be a timestamp string")
        source_generation = entry.get("source_generation")
        if not isinstance(source_generation, str) or not _GENERATION_RE.match(source_generation):
            raise UniverseScdIntegrityError("interval source_generation must be a universe SCD generation id")
        if effective_to is not None and effective_to < effective_from:
            raise UniverseScdIntegrityError("interval ends before it starts")
        normalized.append(
            {
                "effective_from": effective_from,
                "effective_to": effective_to,
                "content_hash": content_hash,
                "available_at": available_at,
                "source_generation": source_generation,
            }
        )
    for previous, current in zip(normalized, normalized[1:]):
        if current["effective_from"] <= previous["effective_from"]:
            raise UniverseScdIntegrityError("interval ledger is not strictly ordered by effective_from")
        if previous["effective_to"] is None:
            raise UniverseScdIntegrityError("open interval must be the last entry")
        if current["effective_from"] <= previous["effective_to"]:
            raise UniverseScdIntegrityError("interval ledger overlaps")
    open_count = sum(1 for interval in normalized if interval["effective_to"] is None)
    if open_count > 1:
        raise UniverseScdIntegrityError("interval ledger has multiple open intervals")
    if open_count == 1 and normalized[-1]["effective_to"] is not None:
        raise UniverseScdIntegrityError("open interval must be the last entry")
    return normalized


def _merge_intervals(existing: list[dict[str, Any]], draft: CollectionDraft) -> tuple[list[dict[str, Any]], bool]:
    """Append the draft interval, closing the previous open interval first.

    Returns the new ledger and whether the draft is an idempotent no-op.
    Same-day (same effective_from) identical source/content is idempotent;
    same-day different content conflicts; the ledger only ever appends.
    """
    merged = [dict(interval) for interval in existing]
    open_interval = next((interval for interval in merged if interval["effective_to"] is None), None)
    if open_interval is not None:
        if open_interval["effective_from"] == draft.effective_from:
            if open_interval["content_hash"] == draft.content_hash:
                return merged, True
            raise UniverseScdConflict(
                "same-day universe collection with different source/content; current publication kept"
            )
        if open_interval["effective_from"] > draft.effective_from:
            raise UniverseScdConflict("new effective_from predates the open interval")
        if draft.prev_market_day is None or draft.prev_market_day < open_interval["effective_from"]:
            raise UniverseScdConflict("cannot close the open interval before its own effective_from")
        open_interval["effective_to"] = draft.prev_market_day
    merged.append(
        {
            "effective_from": draft.effective_from,
            "effective_to": None,
            "content_hash": draft.content_hash,
            "available_at": draft.available_at,
            "source_generation": "00000000T000000Z-0000000000000000",
        }
    )
    return merged, False


# Publisher: parent CAS + flock + fsync + atomic replace
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    status: str
    generation: str
    detail: str | None = None

    def __str__(self) -> str:  # pragma: no cover - logging sugar
        if self.detail:
            return f"{self.status} generation={self.generation} detail={self.detail}"
        return f"{self.status} generation={self.generation}"


def _manifest_digest_core(core: dict[str, Any]) -> dict[str, Any]:
    """Return the digest view; the newest interval points to this generation."""
    digest_core = dict(core)
    intervals = [dict(item) for item in digest_core.get("intervals", [])]
    if intervals:
        intervals[-1]["source_generation"] = ""
        digest_core["intervals"] = intervals
    return digest_core


def _build_manifest(draft: CollectionDraft, intervals: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    serialized_intervals = [
        {
            "effective_from": interval["effective_from"].isoformat(),
            "effective_to": interval["effective_to"].isoformat() if interval["effective_to"] is not None else None,
            "content_hash": interval["content_hash"],
            "available_at": interval["available_at"],
            "source_generation": interval["source_generation"],
        }
        for interval in intervals
    ]
    core: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact": ARTIFACT_NAME,
        "rule_version": RULE_VERSION,
        "status_filter": STATUS_FILTER,
        "available_at": draft.available_at,
        "collection_date": draft.collection_date.isoformat(),
        "effective_from": draft.effective_from.isoformat(),
        "calendar": {
            "identity": draft.calendar_identity,
            "contract": draft.calendar_contract,
            "next_market_day": draft.effective_from.isoformat(),
            "prev_market_day": draft.prev_market_day.isoformat() if draft.prev_market_day is not None else None,
        },
        "source": dict(draft.source),
        "content_hash": draft.content_hash,
        "symbol_count": len(draft.symbols),
        "intervals": serialized_intervals,
    }
    digest = sha256_hex(canonical_json_bytes(_manifest_digest_core(core)))[:16]
    generation = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{digest}"
    core["intervals"][-1]["source_generation"] = generation
    manifest = dict(core)
    manifest["generation"] = generation
    return manifest, generation


def _validate_manifest_contract(manifest: dict[str, Any], generation: str, manifest_bytes: bytes) -> None:
    if manifest.get("generation") != generation:
        raise UniverseScdIntegrityError("manifest generation does not match the pointer")
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise UniverseScdIntegrityError("manifest must be canonical JSON")
    core = dict(manifest)
    core.pop("generation")
    if generation.rsplit("-", 1)[-1] != sha256_hex(canonical_json_bytes(_manifest_digest_core(core)))[:16]:
        raise UniverseScdIntegrityError("generation digest does not match the manifest core")
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("artifact") != ARTIFACT_NAME
        or manifest.get("rule_version") != RULE_VERSION
        or manifest.get("status_filter") != STATUS_FILTER
    ):
        raise UniverseScdIntegrityError("manifest contract mismatch")


def publish_collection(
    root: str | os.PathLike[str],
    data_dir: str | os.PathLike[str],
    draft: CollectionDraft,
) -> PublishOutcome:
    """Publish a collected draft as a new immutable generation (best-effort caller side).

    Fails closed: on any conflict or fault the current pointer is left untouched.
    """
    root = os.fspath(root)
    validate_root_outside_data_dir(root, data_dir)
    os.makedirs(root, exist_ok=True)
    lock_fd = os.open(os.path.join(root, LOCK_FILENAME), os.O_RDWR | os.O_CREAT, 0o644)
    staging_dir: str | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current_bytes = _read_current(root)
        if current_bytes is None:
            existing_intervals: list[dict[str, Any]] = []
            current_generation = ""
            existing_source_generation = None
        else:
            pointer = _require_json(current_bytes, "universe current.json")
            current_generation = pointer.get("generation")
            if not isinstance(current_generation, str) or not _GENERATION_RE.match(current_generation):
                raise UniverseScdIntegrityError("invalid universe current.json generation")
            published = _load_generation_state(root, current_bytes)
            existing_intervals = published["intervals"]
            existing_source_generation = published["manifest"].get("source", {}).get("generation")
        open_interval = next((item for item in existing_intervals if item["effective_to"] is None), None)
        if (
            open_interval is not None
            and open_interval["effective_from"] == draft.effective_from
            and open_interval["content_hash"] == draft.content_hash
            and existing_source_generation != draft.source["generation"]
        ):
            raise UniverseScdConflict("same effective day has different source generation")
        intervals, idempotent = _merge_intervals(existing_intervals, draft)
        if idempotent:
            return PublishOutcome("idempotent", current_generation, "same-day identical source/content")
        _parse_intervals(intervals)
        manifest, generation = _build_manifest(draft, intervals)
        manifest_bytes = canonical_json_bytes(manifest)
        symbols_bytes = canonical_json_bytes(list(draft.symbols))

        staging_dir = os.path.join(root, f".staging-{os.getpid()}-{uuid.uuid4().hex}")
        os.makedirs(staging_dir)
        with open(os.path.join(staging_dir, "symbols.json"), "wb") as fh:
            fh.write(symbols_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        with open(os.path.join(staging_dir, "manifest.json"), "wb") as fh:
            fh.write(manifest_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(staging_dir)

        if _read_current(root) != current_bytes:
            return PublishOutcome("conflict", generation, "expected-current changed during publish")
        final = os.path.join(root, generation)
        if os.path.exists(final):
            try:
                existing_manifest = _read_relative_nofollow(root, f"{generation}/manifest.json")
            except (OSError, UniverseScdIntegrityError):
                return PublishOutcome("name_conflict", generation, "same generation manifest is unreadable")
            if existing_manifest != manifest_bytes:
                return PublishOutcome("name_conflict", generation, "same generation has different manifest bytes")
            import shutil

            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir = None
        else:
            os.replace(staging_dir, final)
            staging_dir = None
        for directory, _, files in os.walk(final, topdown=False):
            for name in files:
                _fsync_file(os.path.join(directory, name))
            _fsync_dir(directory)
        temp = os.path.join(root, f".current.json.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        with open(temp, "wb") as fh:
            fh.write(canonical_json_bytes({"generation": generation}) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, os.path.join(root, CURRENT_FILENAME))
        _fsync_dir(root)
        return PublishOutcome("published", generation)
    finally:
        if staging_dir is not None:
            import shutil

            shutil.rmtree(staging_dir, ignore_errors=True)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# Reader: pinned generation with event-date identity API
# ---------------------------------------------------------------------------


def _load_generation_state(root: str, current_bytes: bytes) -> dict[str, Any]:
    """Fully validate the pointed generation: manifest, digest, ledger, symbol files."""
    pointer = _require_json(current_bytes, "universe current.json")
    generation = pointer.get("generation")
    if not isinstance(generation, str) or not _GENERATION_RE.match(generation):
        raise UniverseScdIntegrityError(f"invalid universe generation pointer: {generation!r}")
    try:
        manifest_bytes = _read_relative_nofollow(root, f"{generation}/manifest.json")
    except FileNotFoundError as exc:
        raise UniverseScdIntegrityError(f"universe generation manifest is missing: {generation}") from exc
    manifest = _require_json(manifest_bytes, "universe generation manifest")
    _validate_manifest_contract(manifest, generation, manifest_bytes)
    intervals = _parse_intervals(manifest.get("intervals"))
    if intervals[-1]["source_generation"] != generation:
        raise UniverseScdIntegrityError("newest interval must reference the current universe generation")
    symbols_bytes = _read_relative_nofollow(root, f"{generation}/symbols.json")
    if sha256_hex(symbols_bytes) != manifest.get("content_hash"):
        raise UniverseScdIntegrityError("universe symbols file does not match the manifest content hash")
    try:
        symbols = json.loads(symbols_bytes)
    except ValueError as exc:
        raise UniverseScdIntegrityError("universe symbols file is not valid JSON") from exc
    if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
        raise UniverseScdIntegrityError("universe symbols file must be a JSON array of strings")
    if symbols != sorted(set(symbols)) or not all(_SYMBOL_RE.match(item) for item in symbols):
        raise UniverseScdIntegrityError("universe symbols file must be sorted, unique canonical A-share symbols")
    if len(symbols) != manifest.get("symbol_count"):
        raise UniverseScdIntegrityError("universe symbol count does not match the manifest")
    symbols_by_hash: dict[str, tuple[str, ...]] = {manifest["content_hash"]: tuple(symbols)}
    for interval in intervals:
        if interval["content_hash"] in symbols_by_hash:
            continue
        source_generation = interval["source_generation"]
        try:
            interval_symbols_bytes = _read_relative_nofollow(root, f"{source_generation}/symbols.json")
        except FileNotFoundError as exc:
            raise UniverseScdIntegrityError(
                f"universe generation holding an interval's symbols is missing: {source_generation}"
            ) from exc
        if sha256_hex(interval_symbols_bytes) != interval["content_hash"]:
            raise UniverseScdIntegrityError(
                f"universe generation symbols do not match interval content hash: {source_generation}"
            )
        try:
            interval_symbols = json.loads(interval_symbols_bytes)
        except ValueError as exc:
            raise UniverseScdIntegrityError(f"universe symbols file is not valid JSON: {source_generation}") from exc
        if (
            not isinstance(interval_symbols, list)
            or not all(isinstance(item, str) and _SYMBOL_RE.match(item) for item in interval_symbols)
            or interval_symbols != sorted(set(interval_symbols))
        ):
            raise UniverseScdIntegrityError(f"universe symbols file is malformed: {source_generation}")
        symbols_by_hash[interval["content_hash"]] = tuple(interval_symbols)
    return {
        "generation": generation,
        "manifest": manifest,
        "manifest_sha256": sha256_hex(manifest_bytes),
        "intervals": intervals,
        "symbols_by_hash": symbols_by_hash,
    }


class PublishedUniverseScdReader:
    """Reader pinned to the currently published generation; never follows later publishes.

    Construction fully validates the pointed generation and every generation
    referenced by the interval ledger. Any fault raises
    ``UniverseScdIntegrityError`` so callers fail closed with a whole-run
    unavailable instead of a partial or fabricated universe.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        data_dir: str | os.PathLike[str] | None = None,
        generation: str | None = None,
        manifest_sha256: str | None = None,
    ) -> None:
        self._root = os.path.realpath(os.fspath(root))
        if data_dir is not None:
            validate_root_outside_data_dir(self._root, data_dir)
        if generation is None:
            try:
                current_bytes = _read_current(self._root)
            except OSError as exc:
                raise UniverseScdIntegrityError("universe current.json is unreadable") from exc
            if current_bytes is None:
                raise UniverseScdNotPublished(
                    "universe SCD is not published yet; no snapshot exists before the first real collection"
                )
        else:
            if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
                raise UniverseScdIntegrityError("invalid pinned universe generation")
            current_bytes = canonical_json_bytes({"generation": generation})
        state = _load_generation_state(self._root, current_bytes)
        resolved_manifest_sha256 = state["manifest_sha256"]
        if manifest_sha256 is not None:
            if not isinstance(manifest_sha256, str) or not _HEX64_RE.fullmatch(
                manifest_sha256.lower()
            ):
                raise UniverseScdIntegrityError("invalid universe manifest pin")
            if resolved_manifest_sha256 != manifest_sha256.lower():
                raise UniverseScdIntegrityError("universe manifest identity mismatch")
        self._generation = state["generation"]
        self._manifest_sha256 = resolved_manifest_sha256
        self._manifest = state["manifest"]
        self._intervals = state["intervals"]
        self._symbols_by_hash = state["symbols_by_hash"]

    def identity(self) -> dict[str, str]:
        return {
            "generation": self._generation,
            "manifest_sha256": self._manifest_sha256,
        }

    def source_manifest(self) -> dict[str, Any]:
        manifest = self._manifest
        return {
            "artifact": manifest.get("artifact"),
            "generation": self._generation,
            "content_hash": manifest.get("content_hash"),
            "available_at": manifest.get("available_at"),
            "effective_from": manifest.get("effective_from"),
            "rule_version": manifest.get("rule_version"),
            "status_filter": manifest.get("status_filter"),
            "source": manifest.get("source"),
            "calendar": manifest.get("calendar"),
        }

    def snapshot_identity(self, event_date: date) -> dict[str, Any]:
        for interval in reversed(self._intervals):
            if interval["effective_from"] <= event_date:
                if interval["effective_to"] is None or event_date <= interval["effective_to"]:
                    return {
                        "content_hash": interval["content_hash"],
                        "effective_from": interval["effective_from"],
                        "effective_to": interval["effective_to"],
                        "available_at": interval["available_at"],
                    }
                break
        raise UniverseScdNoCoverage(f"no universe interval covers {event_date.isoformat()}")

    def eligible_symbols(self, event_date: date) -> list[str]:
        identity = self.snapshot_identity(event_date)
        return list(self._symbols_by_hash[identity["content_hash"]])

    def prefetch_event_days(self, event_days: Iterable[date]) -> dict[date, tuple[dict[str, Any], list[str]]]:
        """Resolve identity + frozen symbols for every request event day in one pass.

        Any integrity fault propagates: the caller must treat the whole run as
        unavailable rather than degrading per event.
        """
        prefetched: dict[date, tuple[dict[str, Any], list[str]]] = {}
        for day in event_days:
            identity = self.snapshot_identity(day)
            prefetched[day] = (identity, list(self._symbols_by_hash[identity["content_hash"]]))
        return prefetched


# ---------------------------------------------------------------------------
# Composition entry point for the daily pipeline hook
# ---------------------------------------------------------------------------


def publish_universe_from_repository(repo: Any, *, now: datetime | None = None) -> PublishOutcome:
    """Collect from the pinned fstore generation and publish best-effort.

    Raises on any fault; the pipeline hook catches and logs, leaving the
    current pointer and every other pipeline step untouched.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    root = universe_scd_root()
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    if data_dir is None:
        raise UniverseScdIntegrityError("repository data_dir unavailable")
    validate_root_outside_data_dir(root, data_dir)
    draft = collect_eligible_universe(now=now)
    outcome = publish_collection(root, data_dir, draft)
    logger.info(
        "universe SCD collection: effective_from=%s calendar=%s source_generation=%s symbols=%d",
        draft.effective_from.isoformat(),
        draft.calendar_identity,
        draft.source["generation"],
        len(draft.symbols),
    )
    return outcome


__all__ = [
    "ARTIFACT_NAME",
    "DEFAULT_UNIVERSE_SCD_ROOT",
    "MANIFEST_SCHEMA_VERSION",
    "RULE_VERSION",
    "STATUS_FILTER",
    "UNIVERSE_SCD_ROOT_ENV",
    "CollectionDraft",
    "PublishOutcome",
    "PublishedUniverseScdReader",
    "UniverseScdConflict",
    "UniverseScdIntegrityError",
    "UniverseScdNoCoverage",
    "UniverseScdNotPublished",
    "canonical_json_bytes",
    "collect_eligible_universe",
    "publish_collection",
    "publish_universe_from_repository",
    "sha256_hex",
    "universe_scd_root",
    "validate_root_outside_data_dir",
]
