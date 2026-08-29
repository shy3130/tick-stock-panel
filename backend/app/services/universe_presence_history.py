"""Retrospective exact-day presence_v1 publisher/reader, schema v2."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import stat
import uuid
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from app.services.universe_scd import (
    FSTORE_LOGICAL,
    FSTORE_ROOT_ENV,
    PublishOutcome,
    canonical_json_bytes,
    fstore_snapshot_root,
    sha256_hex,
    validate_root_outside_data_dir,
)

logger = logging.getLogger(__name__)
PRESENCE_ROOT_ENV = "TICKFLOW_UNIVERSE_PRESENCE_ROOT"
DEFAULT_PRESENCE_ROOT = "/Volumes/WD1/duckdb/snapshots/tickflow-universe-presence"
PRESENCE_CURRENT_FILENAME = "current.json"
PRESENCE_LOCK_FILENAME = ".publish.lock"
PRESENCE_SCHEMA_VERSION = 2
PRESENCE_RULE_VERSION = "presence_v1"
PRESENCE_ARTIFACT_NAME = "universe_presence"
PRESENCE_STATUS_FILTER = "daily_market_row_present_exact_day"
MARKETS_LOGICAL = "markets"
_MARKETS_GENERATION_RE = re.compile(r"^\d{8}T\d{6}$")
_PRESENCE_GENERATION_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^\d{6}$")
_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


class PresenceHistoryError(RuntimeError):
    pass


class PresenceHistoryIntegrityError(PresenceHistoryError):
    pass


class PresenceHistoryNotPublishedError(PresenceHistoryIntegrityError):
    pass


class PresenceHistoryNoCoverageError(PresenceHistoryError):
    pass


class PresenceHistoryNotMarketDayError(PresenceHistoryError):
    pass


class PresenceStatus(StrEnum):
    PRESENT = "present"
    NOT_OBSERVED = "not_observed"


@dataclass(frozen=True, slots=True)
class PresenceDaySnapshot:
    market_day: date
    symbols: tuple[str, ...]
    symbol_count: int
    source_day_observed: bool
    content_hash: str


@dataclass(frozen=True, slots=True)
class PresenceSourcePin:
    root: str
    generation: str
    manifest_sha256: str
    created_at: str | None
    markets_path: str
    markets_size: int
    fstore_path: str
    fstore_size: int

    def identity(self) -> dict[str, Any]:
        return {
            "artifact": "fstore_snapshot",
            "root_env": FSTORE_ROOT_ENV,
            "generation": self.generation,
            "manifest_sha256": self.manifest_sha256,
            "created_at": self.created_at,
            "logicals": {
                MARKETS_LOGICAL: {
                    "logical": MARKETS_LOGICAL,
                    "file": os.path.basename(self.markets_path),
                    "size_bytes": self.markets_size,
                },
                FSTORE_LOGICAL: {
                    "logical": FSTORE_LOGICAL,
                    "file": os.path.basename(self.fstore_path),
                    "size_bytes": self.fstore_size,
                },
            },
        }


@dataclass(frozen=True, slots=True)
class PresenceCollectionDraft:
    published_at: str
    source: dict[str, Any]
    calendar_identity: str
    coverage_start: date
    coverage_end: date
    market_days: tuple[date, ...]
    day_symbols: tuple[tuple[str, ...], ...]
    day_hashes: tuple[str, ...]
    day_observed: tuple[bool, ...]


def _coerce_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _read_fd(fd: int, size: int) -> bytes:
    chunks = []
    total = 0
    while True:
        p = os.read(fd, 1 << 20)
        if not p:
            break
        chunks.append(p)
        total += len(p)
    if total != size:
        raise PresenceHistoryIntegrityError("single-FD size drift")
    return b"".join(chunks)


def _read_relative_nofollow(root: str, relative: str) -> bytes:
    parts = relative.split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise PresenceHistoryIntegrityError("path escape rejected")
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for p in parts[:-1]:
            nxt = os.open(
                p,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=fd,
            )
            os.close(fd)
            fd = nxt
        child = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
    finally:
        os.close(fd)
    try:
        info = os.fstat(child)
        if not stat.S_ISREG(info.st_mode):
            raise PresenceHistoryIntegrityError("artifact is not regular")
        return _read_fd(child, info.st_size)
    finally:
        os.close(child)


def _read_current(root: str) -> bytes | None:
    try:
        return _read_relative_nofollow(root, "current.json")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise PresenceHistoryIntegrityError("current.json is not regular non-symlink") from e


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
        v = json.loads(payload)
    except ValueError as e:
        raise PresenceHistoryIntegrityError(f"{what} invalid JSON") from e
    if not isinstance(v, dict):
        raise PresenceHistoryIntegrityError(f"{what} must be object")
    return v


def _hex(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _HEX64_RE.fullmatch(v):
        raise PresenceHistoryIntegrityError(f"{what} invalid hash")
    return v


def universe_presence_root() -> str:
    return os.environ.get(PRESENCE_ROOT_ENV) or DEFAULT_PRESENCE_ROOT


def _simple(v: Any) -> str:
    if not isinstance(v, str) or not v or "/" in v or v in (".", ".."):
        raise PresenceHistoryIntegrityError("file identity rejected")
    return v


def _pin_entry(root: str, generation: str, entries: Any, logical: str) -> tuple[str, int]:
    if not isinstance(entries, list):
        raise PresenceHistoryIntegrityError("manifest entries malformed")
    e = next((x for x in entries if isinstance(x, dict) and x.get("logical") == logical), None)
    if e is None:
        raise PresenceHistoryIntegrityError(f"missing logical {logical}")
    fn = _simple(e.get("file"))
    gd = os.path.join(root, generation)
    if os.path.islink(gd) or not os.path.isdir(gd):
        raise PresenceHistoryIntegrityError("generation directory invalid")
    path = os.path.join(gd, fn)
    info = None if os.path.islink(path) else os.lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise PresenceHistoryIntegrityError("artifact invalid")
    size = e.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size != info.st_size:
        raise PresenceHistoryIntegrityError("artifact size mismatch")
    return path, size


def pin_presence_source(source_root: str | os.PathLike[str] | None = None) -> PresenceSourcePin:
    root = fstore_snapshot_root() if source_root is None else os.fspath(source_root)
    try:
        pointer = _require_json(_read_relative_nofollow(root, "current.json"), "fstore current")
    except FileNotFoundError:
        raise PresenceHistoryIntegrityError("fstore not published") from None
    gen = pointer.get("generation")
    if not isinstance(gen, str) or not _MARKETS_GENERATION_RE.fullmatch(gen):
        raise PresenceHistoryIntegrityError("invalid fstore generation")
    mb = _read_relative_nofollow(root, f"{gen}/manifest.json")
    m = _require_json(mb, "fstore manifest")
    if m.get("generation") != gen:
        raise PresenceHistoryIntegrityError("fstore generation mismatch")
    mp, ms = _pin_entry(root, gen, m.get("entries"), MARKETS_LOGICAL)
    fp, fs = _pin_entry(root, gen, m.get("entries"), FSTORE_LOGICAL)
    ca = m.get("created_at")
    if ca is not None and not isinstance(ca, str):
        raise PresenceHistoryIntegrityError("created_at invalid")
    return PresenceSourcePin(root, gen, sha256_hex(mb), ca, mp, ms, fp, fs)


def _pinned_market_days(conn: Any) -> tuple[date, ...]:
    rows = conn.execute(
        "SELECT tdate FROM trade_date WHERE isopen = 3 AND mkt = 'A股' ORDER BY tdate"
    ).fetchall()
    out = []
    for r in rows:
        d = _coerce_date(r[0])
        if d is None or (out and d <= out[-1]):
            raise PresenceHistoryIntegrityError("invalid market calendar")
        out.append(d)
    if not out:
        raise PresenceHistoryIntegrityError("empty market calendar")
    return tuple(out)


def _markets_rows_by_day(conn: Any) -> tuple[date, date, dict[date, set[str]]]:
    from app.data_providers.fquant.symbols import code_to_symbol

    rows = conn.execute(
        "SELECT code, trade_date FROM daily_markets WHERE asset_type = 1"
    ).fetchall()
    if not rows:
        raise PresenceHistoryIntegrityError("empty coverage")
    seen = set()
    out = {}
    for code0, day0 in rows:
        day = _coerce_date(day0)
        code = "" if code0 is None else str(code0)
        if day is None or not _CODE_RE.fullmatch(code):
            raise PresenceHistoryIntegrityError("invalid code/date")
        key = (code, day)
        if key in seen:
            raise PresenceHistoryIntegrityError("duplicate key")
        seen.add(key)
        sym = code_to_symbol(code, 1)
        if not _SYMBOL_RE.fullmatch(sym):
            raise PresenceHistoryIntegrityError("invalid mapped symbol")
        out.setdefault(day, set()).add(sym)
    return min(out), max(out), out


def collect_presence_history(
    *, now: datetime | None = None, source_root: str | os.PathLike[str] | None = None
) -> PresenceCollectionDraft:
    now = now or datetime.now(UTC)
    pin = pin_presence_source(source_root)
    from app.storage.duckdb_runtime import connect_duckdb

    mc = connect_duckdb(pin.markets_path, read_only=True)
    try:
        fc = connect_duckdb(pin.fstore_path, read_only=True)
        try:
            calendar = _pinned_market_days(fc)
            start, end, grouped = _markets_rows_by_day(mc)
        finally:
            fc.close()
    finally:
        mc.close()
    days = tuple(d for d in calendar if start <= d <= end)
    if not days or days[0] != start or days[-1] != end:
        raise PresenceHistoryIntegrityError("coverage boundaries not market days")
    if set(grouped) - set(days):
        raise PresenceHistoryIntegrityError("row on non-market day")
    syms = tuple(tuple(sorted(grouped.get(d, ()))) for d in days)
    hashes = tuple(sha256_hex(canonical_json_bytes(list(s))) for s in syms)
    return PresenceCollectionDraft(
        now.astimezone(UTC).isoformat(timespec="seconds"),
        pin.identity(),
        f"fstore_trade_date:{pin.generation}:{pin.manifest_sha256}",
        start,
        end,
        days,
        syms,
        hashes,
        tuple(bool(s) for s in syms),
    )


def _compress(d: PresenceCollectionDraft) -> list[dict[str, Any]]:
    out = []
    start = 0
    for i in range(1, len(d.market_days) + 1):
        if i == len(d.market_days) or d.day_hashes[i] != d.day_hashes[start]:
            h = d.day_hashes[start]
            out.append(
                {
                    "effective_from": d.market_days[start].isoformat(),
                    "effective_to": d.market_days[i - 1].isoformat(),
                    "content_hash": h,
                    "symbols_file": f"symbols/{h}.json",
                    "symbol_count": len(d.day_symbols[start]),
                    "source_day_observed": d.day_observed[start],
                }
            )
            start = i
    return out


def _digest_core(m: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if k not in ("generation", "published_at")}


def _validate_source_identity(raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or raw.get("artifact") != "fstore_snapshot"
        or raw.get("root_env") != FSTORE_ROOT_ENV
    ):
        raise PresenceHistoryIntegrityError("source identity malformed")
    generation = raw.get("generation")
    if not isinstance(generation, str) or not _MARKETS_GENERATION_RE.fullmatch(generation):
        raise PresenceHistoryIntegrityError("source generation malformed")
    _hex(raw.get("manifest_sha256"), "source manifest")
    created_at = raw.get("created_at")
    if created_at is not None and (not isinstance(created_at, str) or not created_at):
        raise PresenceHistoryIntegrityError("source created_at malformed")
    logicals = raw.get("logicals")
    if not isinstance(logicals, dict) or set(logicals) != {MARKETS_LOGICAL, FSTORE_LOGICAL}:
        raise PresenceHistoryIntegrityError("source logical identities malformed")
    for logical in (MARKETS_LOGICAL, FSTORE_LOGICAL):
        entry = logicals[logical]
        if not isinstance(entry, dict) or entry.get("logical") != logical:
            raise PresenceHistoryIntegrityError("source logical identity malformed")
        _simple(entry.get("file"))
        size = entry.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PresenceHistoryIntegrityError("source logical size malformed")
    return raw


def _validate_draft(d: PresenceCollectionDraft) -> None:
    count = len(d.market_days)
    if (
        count == 0
        or len(d.day_symbols) != count
        or len(d.day_hashes) != count
        or len(d.day_observed) != count
    ):
        raise PresenceHistoryIntegrityError("draft arrays are not aligned")
    if (
        list(d.market_days) != sorted(set(d.market_days))
        or d.market_days[0] != d.coverage_start
        or d.market_days[-1] != d.coverage_end
    ):
        raise PresenceHistoryIntegrityError("draft calendar/coverage mismatch")
    if (
        not isinstance(d.published_at, str)
        or not d.published_at
        or not isinstance(d.calendar_identity, str)
        or not d.calendar_identity
    ):
        raise PresenceHistoryIntegrityError("draft identity malformed")
    _validate_source_identity(d.source)
    for symbols, content_hash, observed in zip(
        d.day_symbols, d.day_hashes, d.day_observed, strict=True
    ):
        if symbols != tuple(sorted(set(symbols))) or not all(
            _SYMBOL_RE.fullmatch(symbol) for symbol in symbols
        ):
            raise PresenceHistoryIntegrityError("draft symbols malformed")
        if content_hash != sha256_hex(canonical_json_bytes(list(symbols))) or observed is not bool(
            symbols
        ):
            raise PresenceHistoryIntegrityError("draft symbol identity mismatch")


def _build(
    d: PresenceCollectionDraft, now: datetime
) -> tuple[dict[str, Any], str, list[tuple[str, bytes]]]:
    _validate_draft(d)
    md = canonical_json_bytes([x.isoformat() for x in d.market_days])
    files = {
        h: canonical_json_bytes(list(s)) for h, s in zip(d.day_hashes, d.day_symbols, strict=True)
    }
    core = {
        "schema_version": 2,
        "artifact": PRESENCE_ARTIFACT_NAME,
        "rule_version": PRESENCE_RULE_VERSION,
        "status_filter": PRESENCE_STATUS_FILTER,
        "retrospective": True,
        "source": d.source,
        "calendar": {
            "identity": d.calendar_identity,
            "contract": "fstore_trade_date:mkt=A股,isopen=3,tdate",
        },
        "coverage": {
            "start": d.coverage_start.isoformat(),
            "end": d.coverage_end.isoformat(),
            "market_day_count": len(d.market_days),
        },
        "market_days": {
            "file": "market_days.json",
            "sha256": sha256_hex(md),
            "count": len(d.market_days),
        },
        "intervals": _compress(d),
    }
    gen = f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{sha256_hex(canonical_json_bytes(core))[:16]}"
    m = dict(core)
    m.update(generation=gen, published_at=d.published_at)
    return m, gen, [("market_days.json", md)] + [(f"symbols/{h}.json", b) for h, b in files.items()]


def _stage(root: str, gen: str, mb: bytes, payloads: list[tuple[str, bytes]]) -> str:
    st = os.path.join(root, f".staging-{os.getpid()}-{uuid.uuid4().hex}")
    os.makedirs(os.path.join(st, "symbols"))
    try:
        for rel, p in [("manifest.json", mb), *payloads]:
            path = os.path.join(st, rel)
            with open(path, "wb") as f:
                f.write(p)
                f.flush()
                os.fsync(f.fileno())
        for dr, _, fs in os.walk(st, topdown=False):
            for n in fs:
                _fsync_file(os.path.join(dr, n))
            _fsync_dir(dr)
    except BaseException:
        shutil.rmtree(st, ignore_errors=True)
        raise
    return st


def publish_presence_history(
    root: str | os.PathLike[str],
    data_dir: str | os.PathLike[str],
    *,
    draft: PresenceCollectionDraft | None = None,
    now: datetime | None = None,
    source_root: str | os.PathLike[str] | None = None,
) -> PublishOutcome:
    root = os.fspath(root)
    try:
        validate_root_outside_data_dir(root, data_dir)
    except Exception as e:
        raise PresenceHistoryIntegrityError(str(e)) from e
    now = now or datetime.now(UTC)
    os.makedirs(root, exist_ok=True)
    lock = os.open(os.path.join(root, PRESENCE_LOCK_FILENAME), os.O_RDWR | os.O_CREAT, 0o644)
    staging = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        draft = draft or collect_presence_history(now=now, source_root=source_root)
        m, gen, payloads = _build(draft, now)
        mb = canonical_json_bytes(m)
        cur = _read_current(root)
        curgen = ""
        if cur is not None:
            p = _require_json(cur, "presence current")
            curgen = p.get("generation")
            if not isinstance(curgen, str) or not _PRESENCE_GENERATION_RE.fullmatch(curgen):
                raise PresenceHistoryIntegrityError("invalid current generation")
            old_reader = PublishedPresenceUniverseReader(root, data_dir=data_dir)
            old = old_reader.source_manifest()
            if canonical_json_bytes(_digest_core(old)) == canonical_json_bytes(_digest_core(m)):
                return PublishOutcome("idempotent", curgen, "same source/core")
        staging = _stage(root, gen, mb, payloads)
        if _read_current(root) != cur:
            return PublishOutcome("conflict", gen, "expected-current changed during publish")
        final = os.path.join(root, gen)
        if os.path.exists(final):
            return PublishOutcome("name_conflict", gen, "generation directory already exists")
        os.replace(staging, final)
        staging = None
        for dr, _, fs in os.walk(final, topdown=False):
            for n in fs:
                _fsync_file(os.path.join(dr, n))
            _fsync_dir(dr)
        tmp = os.path.join(root, f".current.tmp-{uuid.uuid4().hex}")
        with open(tmp, "wb") as f:
            f.write(canonical_json_bytes({"generation": gen}) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(root, "current.json"))
        _fsync_dir(root)
        return PublishOutcome("published", gen)
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def _parse_day(v: Any, what: str) -> date:
    d = _coerce_date(v)
    if d is None:
        raise PresenceHistoryIntegrityError(f"{what} invalid")
    return d


class PublishedPresenceUniverseReader:
    def __init__(
        self, root: str | os.PathLike[str], *, data_dir: str | os.PathLike[str] | None = None
    ) -> None:
        self._root = os.path.realpath(os.fspath(root))
        if data_dir is not None:
            try:
                validate_root_outside_data_dir(self._root, data_dir)
            except Exception as e:
                raise PresenceHistoryIntegrityError(str(e)) from e
        cur = _read_current(self._root)
        if cur is None:
            raise PresenceHistoryNotPublishedError("presence not published")
        p = _require_json(cur, "presence current")
        gen = p.get("generation")
        if not isinstance(gen, str) or not _PRESENCE_GENERATION_RE.fullmatch(gen):
            raise PresenceHistoryIntegrityError("invalid generation")
        mb = _read_relative_nofollow(self._root, f"{gen}/manifest.json")
        m = _require_json(mb, "presence manifest")
        if m.get("generation") != gen or canonical_json_bytes(m) != mb:
            raise PresenceHistoryIntegrityError("manifest canonical/generation mismatch")
        schema_version = m.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != PRESENCE_SCHEMA_VERSION
            or any(
                (
                    m.get(key) != expected
                    for key, expected in (
                        ("artifact", PRESENCE_ARTIFACT_NAME),
                        ("rule_version", PRESENCE_RULE_VERSION),
                        ("status_filter", PRESENCE_STATUS_FILTER),
                    )
                )
            )
            or m.get("retrospective") is not True
        ):
            raise PresenceHistoryIntegrityError("manifest contract mismatch")
        if gen.rsplit("-", 1)[-1] != sha256_hex(canonical_json_bytes(_digest_core(m)))[:16]:
            raise PresenceHistoryIntegrityError("generation digest mismatch")
        _validate_source_identity(m.get("source"))
        if not isinstance(m.get("published_at"), str) or not m["published_at"]:
            raise PresenceHistoryIntegrityError("published_at malformed")
        cov, meta = m.get("coverage"), m.get("market_days")
        if (
            not isinstance(cov, dict)
            or not isinstance(meta, dict)
            or meta.get("file") != "market_days.json"
        ):
            raise PresenceHistoryIntegrityError("manifest sections malformed")
        rawb = _read_relative_nofollow(self._root, f"{gen}/market_days.json")
        if sha256_hex(rawb) != _hex(meta.get("sha256"), "market days"):
            raise PresenceHistoryIntegrityError("market days hash mismatch")
        try:
            raw = json.loads(rawb)
        except ValueError as e:
            raise PresenceHistoryIntegrityError("market days invalid JSON") from e
        if (
            canonical_json_bytes(raw) != rawb
            or not isinstance(raw, list)
            or not all(isinstance(x, str) for x in raw)
        ):
            raise PresenceHistoryIntegrityError("market days noncanonical")
        days = tuple(_parse_day(x, "market day") for x in raw)
        start, end = (
            _parse_day(cov.get("start"), "coverage start"),
            _parse_day(cov.get("end"), "coverage end"),
        )
        artifact_day_count = meta.get("count")
        coverage_day_count = cov.get("market_day_count")
        if (
            isinstance(artifact_day_count, bool)
            or not isinstance(artifact_day_count, int)
            or isinstance(coverage_day_count, bool)
            or not isinstance(coverage_day_count, int)
            or not days
            or list(days) != sorted(set(days))
            or days[0] != start
            or days[-1] != end
            or len(days) != artifact_day_count
            or len(days) != coverage_day_count
        ):
            raise PresenceHistoryIntegrityError("market days/count/coverage mismatch")
        idx = {d: i for i, d in enumerate(days)}
        intervals = []
        symbols = {}
        for r in m.get("intervals", []):
            if not isinstance(r, dict):
                raise PresenceHistoryIntegrityError("interval malformed")
            a, b = (
                _parse_day(r.get("effective_from"), "interval start"),
                _parse_day(r.get("effective_to"), "interval end"),
            )
            h = _hex(r.get("content_hash"), "content hash")
            count = r.get("symbol_count")
            obs = r.get("source_day_observed")
            if (
                a > b
                or a not in idx
                or b not in idx
                or r.get("symbols_file") != f"symbols/{h}.json"
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or not isinstance(obs, bool)
            ):
                raise PresenceHistoryIntegrityError("interval malformed")
            if intervals and idx[a] != idx[intervals[-1]["effective_to"]] + 1:
                raise PresenceHistoryIntegrityError("interval gap/overlap")
            if not intervals and a != start:
                raise PresenceHistoryIntegrityError("first interval boundary")
            item = {
                "effective_from": a,
                "effective_to": b,
                "content_hash": h,
                "symbol_count": count,
                "source_day_observed": obs,
            }
            intervals.append(item)
            if h not in symbols:
                sb = _read_relative_nofollow(self._root, f"{gen}/symbols/{h}.json")
                if sha256_hex(sb) != h:
                    raise PresenceHistoryIntegrityError("symbols hash mismatch")
                try:
                    ss = json.loads(sb)
                except ValueError as e:
                    raise PresenceHistoryIntegrityError("symbols invalid JSON") from e
                if (
                    canonical_json_bytes(ss) != sb
                    or not isinstance(ss, list)
                    or ss != sorted(set(ss))
                    or not all(isinstance(x, str) and _SYMBOL_RE.fullmatch(x) for x in ss)
                    or len(ss) != count
                    or bool(ss) != obs
                ):
                    raise PresenceHistoryIntegrityError("symbols contract mismatch")
                symbols[h] = tuple(ss)
            if len(symbols[h]) != count or bool(symbols[h]) != obs:
                raise PresenceHistoryIntegrityError("interval symbol identity mismatch")
        if not intervals or intervals[-1]["effective_to"] != end:
            raise PresenceHistoryIntegrityError("last interval boundary")
        self._generation = gen
        self._manifest = m
        self._market_days = days
        self._day_index = idx
        self._intervals = intervals
        self._starts = [x["effective_from"] for x in intervals]
        self._symbols_by_hash = symbols
        self._coverage_start = start
        self._coverage_end = end

    def source_manifest(self) -> dict[str, Any]:
        return deepcopy(self._manifest)

    def snapshot(self, day: date) -> PresenceDaySnapshot:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise TypeError("day must be date")
        if day < self._coverage_start or day > self._coverage_end:
            raise PresenceHistoryNoCoverageError(day.isoformat())
        if day not in self._day_index:
            raise PresenceHistoryNotMarketDayError(day.isoformat())
        item = self._intervals[bisect_right(self._starts, day) - 1]
        ss = self._symbols_by_hash[item["content_hash"]]
        return PresenceDaySnapshot(
            day, ss, len(ss), item["source_day_observed"], item["content_hash"]
        )

    def presence_status(self, symbol: str, day: date) -> PresenceStatus:
        if not isinstance(symbol, str):
            raise TypeError("symbol must be string")
        return (
            PresenceStatus.PRESENT
            if symbol in self.snapshot(day).symbols
            else PresenceStatus.NOT_OBSERVED
        )

    def prefetch_presence_days(self, days: Iterable[date]) -> Mapping[date, PresenceDaySnapshot]:
        return {d: self.snapshot(d) for d in days}


__all__ = [
    "DEFAULT_PRESENCE_ROOT",
    "PRESENCE_ROOT_ENV",
    "PRESENCE_RULE_VERSION",
    "PRESENCE_SCHEMA_VERSION",
    "PresenceCollectionDraft",
    "PresenceDaySnapshot",
    "PresenceHistoryError",
    "PresenceHistoryIntegrityError",
    "PresenceHistoryNoCoverageError",
    "PresenceHistoryNotMarketDayError",
    "PresenceHistoryNotPublishedError",
    "PresenceSourcePin",
    "PresenceStatus",
    "PublishedPresenceUniverseReader",
    "collect_presence_history",
    "pin_presence_source",
    "publish_presence_history",
    "universe_presence_root",
]
