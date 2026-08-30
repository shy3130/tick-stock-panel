"""Generation-pinned index daily bars for sealed research."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.data_providers.fquant.generation import root_for
from app.storage.duckdb_runtime import connect_duckdb

INDEX_ASSET_TYPE = 10
INDEX_KTYPE = 101
INDEX_FQ = 0
REASON_COVERAGE_INSUFFICIENT = "INDEX_COVERAGE_INSUFFICIENT"
REASON_SOURCE_CONFLICT = "INDEX_SOURCE_CONFLICT"
REASON_SOURCE_UNAVAILABLE = "INDEX_SOURCE_UNAVAILABLE"
_CLOSE_REL_TOL = 1e-6


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class IndexBar(_StrictModel):
    date: date
    close: float
    volume: float | None = None


class IndexLegCoverage(_StrictModel):
    code: str
    klines_rows: int = 0
    klines_min: date | None = None
    klines_max: date | None = None
    markets_rows: int = 0
    markets_min: date | None = None
    markets_max: date | None = None
    markets_tail_rows_merged: int = 0
    markets_overlap_rows_checked: int = 0
    markets_rows_in_base_range_not_merged: int = 0
    close_conflict_rows: int = 0
    merged_min: date | None = None
    merged_max: date | None = None
    merged_rows: int = 0


class IndexLegSeries(_StrictModel):
    code: str
    status: Literal["ok", "unavailable"]
    reason_code: str | None = None
    detail: str | None = None
    bars: list[IndexBar] = []
    coverage: IndexLegCoverage


class IndexDailyPin(_StrictModel):
    klines_generation: str
    klines_manifest_sha256: str
    markets_generation: str
    markets_manifest_sha256: str
    pin_verified: bool
    pin_mode: str


class IndexDailyReadRequest(_StrictModel):
    codes: list[str]
    start: date
    end: date


class IndexDailyPanel(_StrictModel):
    pin: IndexDailyPin
    legs: list[IndexLegSeries]


def _pinned_snapshot(manifest: Mapping[str, Any], logical: str) -> tuple[str, str, str, bool]:
    sources = manifest.get("source_generations")
    pinned = sources.get(logical) if isinstance(sources, Mapping) else None
    expected: str | None = None
    if isinstance(pinned, Mapping):
        generation = pinned.get("generation")
        expected = pinned.get("manifest_sha256")
        if not isinstance(expected, str) or not expected:
            raise ValueError(f"canonical {logical} pin missing manifest_sha256")
    else:
        generation = pinned
    if not isinstance(generation, str) or not generation:
        raise ValueError(f"canonical {logical} generation pin missing")
    root = root_for(logical)
    if not root:
        raise FileNotFoundError(f"{logical} snapshot root unavailable")
    gen_dir = Path(root) / generation
    manifest_path = gen_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FileNotFoundError(f"pinned {logical} manifest unavailable")
    raw = manifest_path.read_bytes()
    try:
        resolved = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"pinned {logical} manifest invalid") from exc
    if not isinstance(resolved, dict) or resolved.get("generation") != generation:
        raise ValueError(f"pinned {logical} manifest mismatch")
    entry = next(
        (
            x
            for x in resolved.get("entries", [])
            if isinstance(x, dict) and x.get("logical") == logical
        ),
        None,
    )
    if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
        raise ValueError(f"pinned {logical} entry missing")
    file_name = Path(entry["file"])
    if file_name.is_absolute() or ".." in file_name.parts:
        raise ValueError(f"pinned {logical} file path invalid")
    db = gen_dir / file_name
    if db.is_symlink() or not db.is_file():
        raise FileNotFoundError(f"pinned {logical} db unavailable")
    resolved_hash = hashlib.sha256(raw).hexdigest()
    if expected is not None and expected != resolved_hash:
        raise ValueError(f"{logical} manifest identity mismatch")
    return str(db), generation, resolved_hash, expected is not None


class PublishedIndexDailyReader:
    """Read index asset_type=10 bars from pinned klines plus markets tail."""

    def __init__(self, klines_path: str, markets_path: str, pin: IndexDailyPin) -> None:
        self._conn = connect_duckdb(klines_path, read_only=True)
        self._lock = threading.Lock()
        self._closed = False
        self._markets_qualifier = "daily_markets"
        if Path(markets_path).resolve() != Path(klines_path).resolve():
            escaped = markets_path.replace("'", "''")
            self._conn.execute(f"ATTACH '{escaped}' AS fstore_markets (READ_ONLY)")
            self._markets_qualifier = "fstore_markets.daily_markets"
        self._pin = pin
        tables = {str(r[0]) for r in self._conn.execute("SHOW TABLES").fetchall()}
        if "day_klines" not in tables:
            self.close()
            raise ValueError("published klines snapshot lacks day_klines")
        cols = {
            str(r[1]).lower()
            for r in self._conn.execute("PRAGMA table_info('day_klines')").fetchall()
        }
        required = {"code", "asset_type", "ktype", "fq", "tdate", "close", "cjl"}
        if not required <= cols:
            self.close()
            raise ValueError("published klines snapshot lacks required columns")

    @classmethod
    def from_canonical_manifest(cls, manifest: Mapping[str, Any]) -> PublishedIndexDailyReader:
        kp, kg, kh, kv = _pinned_snapshot(manifest, "klines")
        mp, mg, mh, mv = _pinned_snapshot(manifest, "markets")
        pin = IndexDailyPin(
            klines_generation=kg,
            klines_manifest_sha256=kh,
            markets_generation=mg,
            markets_manifest_sha256=mh,
            pin_verified=kv and mv,
            pin_mode="manifest_sha256_match" if kv and mv else "missing_expected_hash",
        )
        return cls(kp, mp, pin)

    def pin(self) -> IndexDailyPin:
        return self._pin

    def close(self) -> None:
        if not self._closed:
            try:
                self._conn.close()
            finally:
                self._closed = True

    def __enter__(self) -> PublishedIndexDailyReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _parse(rows: list[dict[str, Any]]) -> dict[date, tuple[float, float | None]]:
        out: dict[date, tuple[float, float | None]] = {}
        for row in rows:
            raw_day = row.get("date")
            try:
                day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
                close = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(close):
                out[day] = (float("nan"), None)
                continue
            volume = row.get("volume")
            try:
                volume = (
                    float(volume) if volume is not None and math.isfinite(float(volume)) else None
                )
            except (TypeError, ValueError):
                volume = None
            out[day] = (close, volume)
        return out

    def _query_klines(
        self, code: str, start: date, end: date
    ) -> dict[date, tuple[float, float | None]]:
        rows = self._conn.execute(
            "SELECT tdate::DATE::text AS date, close::DOUBLE AS close, cjl::DOUBLE AS volume "
            "FROM day_klines WHERE code = ? AND asset_type = 10 AND ktype = 101 AND fq = 0 "
            "AND tdate::DATE BETWEEN ? AND ? ORDER BY tdate::DATE ASC",
            [code, start, end],
        ).fetchall()
        cols = ["date", "close", "volume"]
        return self._parse([dict(zip(cols, row, strict=False)) for row in rows])

    def _query_markets(
        self, code: str, start: date, end: date
    ) -> dict[date, tuple[float, float | None]]:
        cursor = self._conn.execute(f"SELECT * FROM {self._markets_qualifier} LIMIT 0")
        names = {str(x[0]).lower(): str(x[0]) for x in cursor.description}
        dcol = names.get("trade_date") or names.get("tdate")
        if not dcol or "code" not in names or "asset_type" not in names or "price" not in names:
            raise ValueError("published markets snapshot lacks index quote columns")
        if "cjl" in names:
            vol = f"CAST({names['cjl']} AS DOUBLE)"
        elif "payload_json" in names:
            vol = "CAST(NULLIF(payload_json->>'Cjl', '') AS DOUBLE)"
        else:
            vol = "NULL"
        rows = self._conn.execute(
            f"SELECT {dcol}::DATE::text AS date, price::DOUBLE AS close, {vol} AS volume "
            f"FROM {self._markets_qualifier} WHERE code = ? AND asset_type = 10 "
            f"AND {dcol}::DATE BETWEEN ? AND ? ORDER BY {dcol}::DATE ASC",
            [code, start, end],
        ).fetchall()
        return self._parse(
            [dict(zip(["date", "close", "volume"], row, strict=False)) for row in rows]
        )

    def read_index_daily(
        self, request: IndexDailyReadRequest | Mapping[str, Any]
    ) -> IndexDailyPanel:
        req = (
            request
            if isinstance(request, IndexDailyReadRequest)
            else IndexDailyReadRequest.model_validate(request)
        )
        if req.start > req.end:
            raise ValueError("start must be <= end")
        legs: list[IndexLegSeries] = []
        for code in sorted(set(req.codes)):
            coverage = IndexLegCoverage(code=code)
            try:
                k = self._query_klines(code, req.start, req.end)
                m = self._query_markets(code, req.start, req.end)
                coverage = coverage.model_copy(
                    update={
                        "klines_rows": len(k),
                        "klines_min": min(k) if k else None,
                        "klines_max": max(k) if k else None,
                        "markets_rows": len(m),
                        "markets_min": min(m) if m else None,
                        "markets_max": max(m) if m else None,
                    }
                )
                if not k:
                    legs.append(
                        IndexLegSeries(
                            code=code,
                            status="unavailable",
                            reason_code=REASON_COVERAGE_INSUFFICIENT,
                            detail="no klines base rows",
                            coverage=coverage,
                        )
                    )
                    continue
                base_max = max(k)
                conflicts = []
                not_merged = 0
                tail: dict[date, tuple[float, float | None]] = {}
                for day, value in m.items():
                    if day > base_max:
                        tail[day] = value
                    elif day in k:
                        coverage = coverage.model_copy(
                            update={
                                "markets_overlap_rows_checked": coverage.markets_overlap_rows_checked
                                + 1
                            }
                        )
                        a, b = k[day][0], value[0]
                        if (
                            not math.isfinite(a)
                            or not math.isfinite(b)
                            or abs(a - b) > _CLOSE_REL_TOL * max(1.0, abs(a), abs(b))
                        ):
                            conflicts.append(day)
                    else:
                        not_merged += 1
                coverage = coverage.model_copy(
                    update={
                        "markets_tail_rows_merged": len(tail),
                        "markets_rows_in_base_range_not_merged": not_merged,
                        "close_conflict_rows": len(conflicts),
                    }
                )
                if conflicts:
                    legs.append(
                        IndexLegSeries(
                            code=code,
                            status="unavailable",
                            reason_code=REASON_SOURCE_CONFLICT,
                            detail=f"close mismatch on {len(conflicts)} overlap rows",
                            coverage=coverage,
                        )
                    )
                    continue
                merged = dict(k)
                merged.update(tail)
                bars = [
                    IndexBar(date=d, close=v[0], volume=v[1])
                    for d, v in sorted(merged.items())
                    if math.isfinite(v[0])
                ]
                coverage = coverage.model_copy(
                    update={
                        "merged_min": bars[0].date if bars else None,
                        "merged_max": bars[-1].date if bars else None,
                        "merged_rows": len(bars),
                    }
                )
                if not bars:
                    legs.append(
                        IndexLegSeries(
                            code=code,
                            status="unavailable",
                            reason_code=REASON_COVERAGE_INSUFFICIENT,
                            coverage=coverage,
                        )
                    )
                else:
                    legs.append(
                        IndexLegSeries(code=code, status="ok", bars=bars, coverage=coverage)
                    )
            except Exception as exc:  # fail closed per leg
                legs.append(
                    IndexLegSeries(
                        code=code,
                        status="unavailable",
                        reason_code=REASON_SOURCE_UNAVAILABLE,
                        detail=str(exc),
                        coverage=coverage,
                    )
                )
        return IndexDailyPanel(pin=self._pin, legs=legs)


__all__ = [
    "REASON_COVERAGE_INSUFFICIENT",
    "REASON_SOURCE_CONFLICT",
    "REASON_SOURCE_UNAVAILABLE",
    "IndexBar",
    "IndexDailyPanel",
    "IndexDailyPin",
    "IndexDailyReadRequest",
    "IndexLegCoverage",
    "IndexLegSeries",
    "PublishedIndexDailyReader",
]
