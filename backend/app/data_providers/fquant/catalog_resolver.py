"""Resolve dated DuckDB routes from engine's published catalog.

Catalog routes pin an exact generation and file.  Resolution deliberately has
no cache and no raw-file fallback: callers either receive the catalog-selected
immutable file or a :class:`CatalogError`.

A *staged* catalog (rows carrying ``stage`` = ``preliminary``/``final``) is
a precondition for ``require_current`` routes: only a staged row can prove which
generation a live read should pin.  Legacy ``stage=NULL`` ``require_current``
rows stay fail-closed and surface staged-migration guidance rather than a bare
historical-fallback error.  Publish order and safe-rollback constraints are
documented in ``AGENTS.md`` ("catalog/engine 发布顺序（staged 迁移运维）").
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT_ENV: dict[str, str] = {
    "/Volumes/WD1/duckdb/snapshots/fstore": "FQUANT_SNAPSHOT_ROOT_FSTORE",
    "/Volumes/WD1/duckdb/snapshots/engine-a": "FQUANT_SNAPSHOT_ROOT_ENGINE_A",
    "/Volumes/WD1/duckdb/snapshots/engine-a-preliminary": (
        "FQUANT_SNAPSHOT_ROOT_ENGINE_A_PRELIMINARY"
    ),
    "/Volumes/WD1/duckdb/snapshots/engine-hk": "FQUANT_SNAPSHOT_ROOT_ENGINE_HK",
    "/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive": (
        "FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE"
    ),
    "/Volumes/WD1/duckdb/snapshots/engine-a-minutes-archive": (
        "FQUANT_SNAPSHOT_ROOT_ENGINE_A_MINUTES_ARCHIVE"
    ),
}
_CATALOG_ROOT_DEFAULT = "/Volumes/WD1/duckdb/snapshots/catalog"
_CATALOG_LOGICAL = "duckdb_catalog"

FRESHNESS_PINNED_IMMUTABLE = "pinned_immutable"
FRESHNESS_REQUIRE_CURRENT = "require_current"

_PRELIMINARY_ROUTE_KEYS = {
    "tdx_minutes": "tdx_minutes_preliminary",
    "tdx_trans": "tdx_trans_preliminary",
}

# Guidance surfaced verbatim in fail-closed diagnostics. Two distinct classes,
# kept separate so an operator can tell a legacy-catalog precondition from a
# stale-generation republish need:
# * legacy (stage=NULL) require_current -> republish as staged first.
# * staged (final/preliminary) stale generation -> republish a row pinning the
#   root's current generation (the route is already staged, no migration needed).
_STAGE_MIGRATION_GUIDANCE = (
    "publish this route as staged (preliminary then final) via the engine "
    "catalog publisher; a stage=NULL require_current row is unsafe until then "
    "(see AGENTS.md 'catalog/engine 发布顺序（staged 迁移运维）')"
)
_STALE_REPUBLISH_GUIDANCE = "republish a catalog row that pins the root's current generation"


class CatalogError(RuntimeError):
    """The catalog could not provide a safe, fresh snapshot file."""


class RouteNotFoundError(CatalogError):
    """No catalog row matched the requested route and date."""


class StaleCatalogError(CatalogError):
    """A require-current route does not pin the root's current generation."""


def _catalog_root() -> str:
    return os.getenv("FQUANT_SNAPSHOT_ROOT_CATALOG", _CATALOG_ROOT_DEFAULT)


def _physical_root(canonical: str) -> str:
    env = _ROOT_ENV.get(canonical)
    if env is None:
        raise CatalogError(f"unknown catalog root {canonical!r}")
    return (os.getenv(env) or "").strip() or canonical


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise CatalogError(f"not a regular file: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except CatalogError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"expected JSON object in {path}")
    return value


def _safe_generation(value: object) -> str:
    if not isinstance(value, str):
        raise CatalogError(f"unsafe generation {value!r}")
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError as exc:
        raise CatalogError(f"unsafe generation {value!r}") from exc
    if parsed.strftime("%Y%m%dT%H%M%S") != value:
        raise CatalogError(f"unsafe generation {value!r}")
    return value


def _safe_relative_file(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"unsafe file {value!r}")
    relative = Path(value)
    if relative.is_absolute() or relative.parts in {(), (".",)} or ".." in relative.parts:
        raise CatalogError(f"unsafe file {value!r}")
    return relative


def _current_generation(root: str) -> str:
    pointer = _read_json(Path(root) / "current.json")
    return _safe_generation(pointer.get("generation"))


def _resolve_pinned(root: str, generation: object, logical: object, file: object) -> str:
    """Resolve one exact generation without consulting its current pointer."""
    safe_generation = _safe_generation(generation)
    if not isinstance(logical, str) or not logical:
        raise CatalogError(f"invalid logical {logical!r}")
    relative_file = _safe_relative_file(file)
    generation_dir = Path(root) / safe_generation
    if generation_dir.is_symlink() or not generation_dir.is_dir():
        raise CatalogError(f"generation is not a regular directory: {generation_dir}")
    manifest = _read_json(generation_dir / "manifest.json")
    if manifest.get("generation") != safe_generation:
        raise CatalogError(
            f"manifest generation {manifest.get('generation')!r} != {safe_generation!r}"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise CatalogError(f"manifest entries are invalid: {generation_dir}")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("logical") != logical:
            continue
        manifest_file = _safe_relative_file(entry.get("file"))
        if manifest_file != relative_file:
            raise CatalogError(
                f"logical {logical} pins {relative_file} but manifest has {manifest_file}"
            )
        path = generation_dir
        for component in relative_file.parts:
            path /= component
            if path.is_symlink():
                raise CatalogError(f"unsafe symlink component: {path}")
        if not path.is_file():
            raise CatalogError(f"pinned file is missing or not regular: {path}")
        size = entry.get("size_bytes", 0)
        if isinstance(size, int) and size and path.stat().st_size != size:
            raise CatalogError(
                f"pinned file size {path.stat().st_size} != manifest size {size}: {path}"
            )
        return str(path)
    raise CatalogError(f"logical {logical} not found in generation {safe_generation} of {root}")


def _catalog_db_path() -> str:
    root = _catalog_root()
    generation = _current_generation(root)
    return _resolve_pinned(root, generation, _CATALOG_LOGICAL, "catalog.duckdb")


def _coerce_trade_date(value: date | datetime | str | None) -> date:
    if value is None:
        raise CatalogError("trade_date is required for date-sharded routes")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise CatalogError(f"invalid trade_date {value!r}") from exc
        if parsed.isoformat() != value:
            raise CatalogError(f"invalid trade_date {value!r}")
        return parsed
    raise CatalogError(f"invalid trade_date {value!r}")


def _optional_date(value: object, field: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise CatalogError(f"route has invalid {field} {value!r}") from exc
        if parsed.isoformat() != value:
            raise CatalogError(f"route has invalid {field} {value!r}")
        return parsed
    raise CatalogError(f"route has invalid {field} {value!r}")


def _route_rows_for_keys(route_keys: list[str], market: str) -> dict[str, list[dict[str, Any]]]:
    if not route_keys:
        return {}
    from app.storage.duckdb_runtime import connect_duckdb

    try:
        # Pin the catalog generation once per request.  Do not resolve the
        # catalog separately for final, preliminary, and historical queries:
        # a publish between those reads could otherwise mix two generations.
        connection = connect_duckdb(_catalog_db_path(), read_only=True)
        try:
            placeholders = ", ".join("?" for _ in route_keys)
            result = connection.execute(
                f"SELECT * FROM catalog_routes WHERE route_key IN ({placeholders}) AND market = ?",
                [*route_keys, market],
            )
            columns = [str(item[0]) for item in result.description]
            grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in route_keys}
            for row in result.fetchall():
                mapped = dict(zip(columns, row, strict=True))
                grouped.setdefault(str(mapped.get("route_key")), []).append(mapped)
            return grouped
        finally:
            connection.close()
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError(f"cannot query route catalog: {exc}") from exc


def _route_stage(row: dict[str, Any]) -> str:
    value = row.get("stage")
    return "" if value is None else str(value)


def _validate_route_metadata(row: dict[str, Any]) -> None:
    stage = _route_stage(row)
    generation = row.get("generation")
    preliminary_root = row.get("preliminary_root_id")
    preliminary_generation = row.get("preliminary_generation")
    if (preliminary_root in (None, "")) != (preliminary_generation in (None, "")):
        raise CatalogError("route preliminary root and generation must be provided together")
    if stage:
        _safe_generation(generation)
    if preliminary_generation not in (None, ""):
        _safe_generation(preliminary_generation)
    supersedes = row.get("supersedes")
    if supersedes not in (None, ""):
        _safe_generation(supersedes)
        if supersedes == generation:
            raise CatalogError("route supersedes its own generation")
    coverage = _optional_date(row.get("coverage_date"), "coverage_date")
    if stage == "":
        if coverage is not None or any(
            row.get(key) not in (None, "", False, 0)
            for key in (
                "reconciled",
                "quality",
                "reconciliation_ref",
                "supersedes",
                "preliminary_root_id",
                "preliminary_generation",
            )
        ):
            raise CatalogError("legacy route carries staged metadata")
        return
    if stage == "preliminary":
        if coverage is None or bool(row.get("reconciled")) or row.get("quality") != "preliminary":
            raise CatalogError("preliminary route has invalid staged metadata")
        if any(
            row.get(key) not in (None, "", False, 0)
            for key in (
                "reconciliation_ref",
                "supersedes",
                "preliminary_root_id",
                "preliminary_generation",
            )
        ):
            raise CatalogError("preliminary route has invalid staged metadata")
        return
    if stage == "final":
        if coverage is None or not bool(row.get("reconciled")) or row.get("quality") != "verified":
            raise CatalogError("final route has invalid staged metadata")
        if not isinstance(row.get("reconciliation_ref"), str) or not row["reconciliation_ref"]:
            raise CatalogError("final route has invalid staged metadata")
        for key in ("supersedes", "preliminary_generation"):
            if row.get(key) not in (None, ""):
                _safe_generation(row[key])
        return
    raise CatalogError(f"route has unknown stage {stage!r}")


def _matches_span(row: dict[str, Any], requested: date) -> bool:
    start = _optional_date(row.get("start_date"), "start_date")
    end = _optional_date(row.get("end_date"), "end_date")
    if start is not None and end is not None and end < start:
        raise CatalogError("route has end_date before start_date")
    return (start is None or start <= requested) and (end is None or requested <= end)


def _priority(row: dict[str, Any]) -> int:
    try:
        return int(row.get("priority") or 0)
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"route has invalid priority {row.get('priority')!r}") from exc


def _resolve_row(
    row: dict[str, Any], route_key: str, market: str, requested: date, *, historical: bool
) -> str:
    _validate_route_metadata(row)
    canonical_root = row.get("root")
    root = _physical_root(canonical_root)
    generation = _safe_generation(row.get("generation"))
    logical = row.get("logical")
    if not isinstance(logical, str) or not logical:
        raise CatalogError(f"{route_key}/{market} has invalid logical {logical!r}")
    file = row.get("file")
    _safe_relative_file(file)
    freshness = row.get("freshness_mode")
    stage = _route_stage(row)
    if freshness == FRESHNESS_REQUIRE_CURRENT:
        if not stage:
            # A legacy (stage=NULL) require_current row cannot prove which
            # generation a live read should pin, so it is unsafe as either an
            # exact match or a historical fallback. Fail closed with the staged
            # migration precondition instead of silently serving a snapshot.
            raise CatalogError(
                f"{route_key}/{market} is a legacy (stage=NULL) require_current "
                f"route and cannot serve a safe snapshot; {_STAGE_MIGRATION_GUIDANCE}"
            )
        if historical:
            coverage = _optional_date(row.get("coverage_date"), "coverage_date")
            if coverage is None or requested >= coverage:
                raise CatalogError(
                    f"{route_key}/{market} coverage_date does not strictly follow requested {requested}"
                )
        current = _current_generation(root)
        if current != generation:
            # The route is already staged (final/preliminary); only its pinned
            # generation has drifted from the root's current pointer. Give the
            # accurate republish guidance, not the legacy-migration guidance.
            raise StaleCatalogError(
                f"{route_key}/{market} pins {generation} but {root} is at {current}; "
                f"the catalog is stale — {_STALE_REPUBLISH_GUIDANCE}"
            )
    elif freshness != FRESHNESS_PINNED_IMMUTABLE:
        raise CatalogError(f"{route_key}/{market} has unknown freshness_mode {freshness!r}")
    if freshness == FRESHNESS_PINNED_IMMUTABLE and stage:
        raise CatalogError(f"{route_key}/{market} staged route cannot be pinned_immutable")
    return _resolve_pinned(root, generation, logical, file)


def resolve_route(route_key: str, market: str, trade_date: date | datetime | str | None) -> str:
    """Resolve an exact final route, then a safe later final or preliminary route.

    Catalog state is read on every call. A dated lookup never falls back to a
    writer-owned raw file.
    """
    requested = _coerce_trade_date(trade_date)
    preliminary_key = _PRELIMINARY_ROUTE_KEYS.get(route_key)
    route_keys = [route_key] + ([preliminary_key] if preliminary_key else [])
    rows_by_key = _route_rows_for_keys(route_keys, market)
    rows = rows_by_key.get(route_key, [])
    final_candidates = []
    for row in rows_by_key.get(route_key, []):
        _validate_route_metadata(row)
        if _route_stage(row) != "final":
            continue
        if _optional_date(row.get("coverage_date"), "coverage_date") != requested:
            continue
        if _matches_span(row, requested):
            final_candidates.append(row)
    if final_candidates:
        final_candidates.sort(
            key=lambda item: (
                -_priority(item),
                str(item.get("root", "")),
                str(item.get("generation", "")),
                str(item.get("logical", "")),
                str(item.get("file", "")),
            )
        )
        return _resolve_row(final_candidates[0], route_key, market, requested, historical=False)

    final_candidates = []
    for row in rows_by_key.get(route_key, []):
        _validate_route_metadata(row)
        if _route_stage(row) != "final":
            continue
        coverage = _optional_date(row.get("coverage_date"), "coverage_date")
        if coverage is None or coverage <= requested:
            continue
        if _matches_span(row, requested):
            final_candidates.append(row)
    if final_candidates:
        final_candidates.sort(
            key=lambda item: (
                -_priority(item),
                _optional_date(item.get("coverage_date"), "coverage_date"),
                str(item.get("root", "")),
                str(item.get("generation", "")),
                str(item.get("logical", "")),
                str(item.get("file", "")),
            )
        )
        return _resolve_row(final_candidates[0], route_key, market, requested, historical=True)

    if preliminary_key:
        preliminary_candidates = []
        for row in rows_by_key.get(preliminary_key, []):
            _validate_route_metadata(row)
            if _route_stage(row) != "preliminary":
                continue
            if _optional_date(row.get("coverage_date"), "coverage_date") != requested:
                continue
            if _matches_span(row, requested):
                preliminary_candidates.append(row)
        if preliminary_candidates:
            preliminary_candidates.sort(
                key=lambda item: (
                    -_priority(item),
                    str(item.get("root", "")),
                    str(item.get("generation", "")),
                    str(item.get("logical", "")),
                    str(item.get("file", "")),
                )
            )
            return _resolve_row(
                preliminary_candidates[0], route_key, market, requested, historical=False
            )

    if not rows:
        raise RouteNotFoundError(f"no catalog route for {route_key}/{market} on {requested}")
    rows = [row for row in rows if _matches_span(row, requested)]
    if not rows:
        raise RouteNotFoundError(f"no catalog route for {route_key}/{market} on {requested}")
    rows.sort(
        key=lambda item: (
            -_priority(item),
            str(item.get("root", "")),
            str(item.get("generation", "")),
            str(item.get("logical", "")),
            str(item.get("file", "")),
        )
    )
    return _resolve_row(rows[0], route_key, market, requested, historical=True)


def latest_route_coverage(route_key: str, market: str) -> dict[str, Any] | None:
    """Return the newest safely readable staged coverage without scanning data tables."""
    preliminary_key = _PRELIMINARY_ROUTE_KEYS.get(route_key)
    route_keys = [route_key] + ([preliminary_key] if preliminary_key else [])
    rows_by_key = _route_rows_for_keys(route_keys, market)
    candidates: list[tuple[date, int, int, dict[str, Any]]] = []
    for key in route_keys:
        for row in rows_by_key.get(key, []):
            _validate_route_metadata(row)
            stage = _route_stage(row)
            if stage not in {"final", "preliminary"}:
                continue
            coverage = _optional_date(row.get("coverage_date"), "coverage_date")
            if coverage is None or not _matches_span(row, coverage):
                continue
            candidates.append(
                (coverage, 1 if stage == "final" else 0, _priority(row), row)
            )
    if not candidates:
        return None
    coverage, _, _, selected = max(
        candidates,
        key=lambda item: (
            item[0],
            item[1],
            item[2],
            str(item[3].get("generation", "")),
        ),
    )
    path = _resolve_row(
        selected, route_key, market, coverage, historical=False
    )
    return {
        "latest_date": coverage.isoformat(),
        "stage": _route_stage(selected),
        "generation": str(selected.get("generation", "")),
        "logical": str(selected.get("logical", "")),
        "path": path,
    }
