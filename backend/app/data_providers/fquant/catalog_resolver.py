"""Resolve dated DuckDB routes from engine's published catalog.

Catalog routes pin an exact generation and file.  Resolution deliberately has
no cache and no raw-file fallback: callers either receive the catalog-selected
immutable file or a :class:`CatalogError`.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT_ENV: dict[str, str] = {
    "/Volumes/WD1/snapshots/fstore": "FQUANT_SNAPSHOT_ROOT_FSTORE",
    "/Volumes/WD1/snapshots/engine-a": "FQUANT_SNAPSHOT_ROOT_ENGINE_A",
    "/Volumes/WD1/snapshots/engine-hk": "FQUANT_SNAPSHOT_ROOT_ENGINE_HK",
    "/Volumes/WD1/snapshots/engine-a-trans-archive": (
        "FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE"
    ),
    "/Volumes/WD1/snapshots/engine-a-minutes-archive": (
        "FQUANT_SNAPSHOT_ROOT_ENGINE_A_MINUTES_ARCHIVE"
    ),
}
_CATALOG_ROOT_DEFAULT = "/Volumes/WD1/snapshots/catalog"
_CATALOG_LOGICAL = "duckdb_catalog"

FRESHNESS_PINNED_IMMUTABLE = "pinned_immutable"
FRESHNESS_REQUIRE_CURRENT = "require_current"


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


def resolve_route(route_key: str, market: str, trade_date: date | None) -> str:
    """Resolve a route to its catalog-pinned immutable DuckDB file.

    The catalog is re-read on every call. Any failure raises ``CatalogError``;
    callers must not fall back to a writer-owned raw database.
    """
    import duckdb

    try:
        connection = duckdb.connect(_catalog_db_path(), read_only=True)
        try:
            if trade_date is None:
                dated_row = connection.execute(
                    "SELECT count(*) FROM catalog_routes "
                    "WHERE route_key = ? AND market = ? "
                    "AND (start_date IS NOT NULL OR end_date IS NOT NULL)",
                    [route_key, market],
                ).fetchone()
                if dated_row and dated_row[0]:
                    raise CatalogError(
                        f"{route_key}/{market} is date-sharded; trade_date is required"
                    )
            row = connection.execute(
                """SELECT root, generation, logical, file, freshness_mode
                     FROM catalog_routes
                    WHERE route_key = ? AND market = ?
                      AND (start_date IS NULL OR (? IS NOT NULL AND start_date <= CAST(? AS DATE)))
                      AND (end_date IS NULL OR (? IS NOT NULL AND end_date >= CAST(? AS DATE)))
                    ORDER BY priority DESC
                    LIMIT 1""",
                [route_key, market, trade_date, trade_date, trade_date, trade_date],
            ).fetchone()
        finally:
            connection.close()
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError(f"cannot query route catalog: {exc}") from exc

    if row is None:
        raise RouteNotFoundError(f"no catalog route for {route_key}/{market} on {trade_date}")

    canonical_root, generation, logical, file, freshness = row
    root = _physical_root(canonical_root)
    generation = _safe_generation(generation)
    _safe_relative_file(file)
    if freshness == FRESHNESS_REQUIRE_CURRENT:
        current = _current_generation(root)
        if current != generation:
            raise StaleCatalogError(
                f"{route_key}/{market} pins {generation} but {root} is at {current}; "
                "the catalog needs republishing"
            )
    elif freshness != FRESHNESS_PINNED_IMMUTABLE:
        raise CatalogError(f"{route_key}/{market} has unknown freshness_mode {freshness!r}")
    return _resolve_pinned(root, generation, logical, file)
