"""Pure-Python mirror of engine's ``pkg/snapshot``.

Maps a known production *raw* DuckDB path to the immutable file of its snapshot
root's current generation, so the panel reads a stable snapshot instead of a
file that engine/fstore writers are actively mutating.

On-disk layout written by ``duckdbsnap`` (see engine/duckdbsnap/manifest.go)::

    <root>/current.json               -> {"generation": "20260710T153000"}
    <root>/<generation>/manifest.json -> {"generation", "created_at",
                                          "entries": [{"logical","file","size_bytes"}]}
    snapshot file = <root>/<generation>/<file>

Any failure (no snapshot published, unknown/``-web`` path, malformed manifest,
missing file) yields a *raw fallback*: the caller keeps using the raw path and
nothing breaks before or without a publish.
"""
from __future__ import annotations

import json
import os
import re
import time

# Canonical per-domain snapshot roots (must match engine pkg/snapshot).
ROOT_FSTORE = "/Volumes/WD1/duckdb/snapshots/fstore"
ROOT_ENGINE_A = "/Volumes/WD1/duckdb/snapshots/engine-a"
ROOT_ENGINE_HK = "/Volumes/WD1/duckdb/snapshots/engine-hk"
# Dedicated whole-DB roots that engine/fstore publish separately from the
# shared fstore / engine-a generations. Keeping these on their own root means
# a republish of one cannot swap the file read for the other logical, and a
# missing publish on the shared root cannot shadow them (Contract A).
ROOT_FSTORE_EXTENDED = "/Volumes/WD1/duckdb/snapshots/fstore-extended"
ROOT_ENGINE_A_CALLAUCTION = "/Volumes/WD1/duckdb/snapshots/engine-a-callauction"
ROOT_ENGINE_A_MONEYFLOW_MINUTE = "/Volumes/WD1/duckdb/snapshots/engine-a-moneyflow-minute"
# Date-sharded archive roots (engine-a-trans-archive / engine-a-minutes-archive)
# are intentionally NOT exposed here: their logicals are date-sharded and must
# resolve only through catalog_resolver.resolve_route, which pins an exact
# generation and validates the trade-date span. A static snapshot_or_raw entry
# would bypass that and serve a snapshot for the wrong date/generation.

# resolve() is called on every TdxDuckDBClient query (see
# tdx_duckdb_client.py's _LeasedSource._resolve, which re-resolves the
# generation per query so a live snapshot swap is picked up without a
# restart). Each call does two file opens + JSON parses; measured ~28us on a
# warm page cache, which is negligible for a single query but adds up under
# tight loops (e.g. a screener/backtest issuing many sequential per-symbol
# queries). A short TTL cache removes that repeated I/O while keeping the
# generation-swap detection window well under anything operationally
# meaningful (snapshots publish at most every few minutes).
_CACHE_TTL_SECONDS = 1.5
_cache: dict[tuple[str, str], tuple[float, str | None]] = {}

# Raw production path -> (root, logical). Mirrors engine pkg/snapshot.rawTargets.
# Only raw paths are mapped here so snapshot_or_raw() resolves them to the
# published generation snapshot; ``-web`` paths are deliberately NOT mapped
# (they would bypass the snapshot and serve a stale separate copy), so clients
# must default to raw paths, not ``-web``.
_RAW_TARGETS: dict[str, tuple[str, str]] = {
    "/Volumes/WD1/duckdb/tdx.duckdb": (ROOT_ENGINE_A, "tdx"),
    # A 股 minutes (tdx-minutes/*) are intentionally absent: they are
    # date-sharded and must resolve through catalog_resolver.resolve_route,
    # never via this static snapshot_or_raw bypass.
    "/Volumes/WD1/duckdb/tdx-chip.duckdb": (ROOT_ENGINE_A, "tdx_chip"),
    "/Volumes/WD1/duckdb/tdx-moneyflow.duckdb": (ROOT_ENGINE_A, "tdx_moneyflow"),
    "/Volumes/WD1/duckdb/tdx-moneyflow-minute.duckdb": (ROOT_ENGINE_A_MONEYFLOW_MINUTE, "tdx_moneyflow_minute"),
    "/Volumes/WD1/duckdb/tdx-hk.duckdb": (ROOT_ENGINE_HK, "tdx_hk"),
    "/Volumes/WD1/duckdb/tdx-hkminutes.duckdb": (ROOT_ENGINE_HK, "tdx_hk_minutes"),
    "/Volumes/WD1/duckdb/tdx-hktrans.duckdb": (ROOT_ENGINE_HK, "tdx_hk_trans"),
    "/Volumes/WD1/duckdb/fstore.duckdb": (ROOT_FSTORE, "fstore"),
    "/Volumes/WD1/duckdb/fstore-markets.duckdb": (ROOT_FSTORE, "markets"),
    "/Volumes/WD1/duckdb/fstore-klines.duckdb": (ROOT_FSTORE, "klines"),
    "/Volumes/WD1/duckdb/fstore-minutes.duckdb": (ROOT_FSTORE, "minutes"),
    "/Volumes/WD1/duckdb/fstore-extended.duckdb": (ROOT_FSTORE_EXTENDED, "extended"),
}

# Keep raw-path consumers (notably FStoreDuckDBClient) configurable too.
# generation.current_path() has the same overrides for its logical consumers,
# but importing generation here would create a cycle.
_ROOT_ENV: dict[str, str] = {
    ROOT_FSTORE: "FQUANT_SNAPSHOT_ROOT_FSTORE",
    ROOT_FSTORE_EXTENDED: "FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED",
    ROOT_ENGINE_A: "FQUANT_SNAPSHOT_ROOT_ENGINE_A",
    ROOT_ENGINE_A_MONEYFLOW_MINUTE: "FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE",
    ROOT_ENGINE_HK: "FQUANT_SNAPSHOT_ROOT_ENGINE_HK",
}

_GENERATION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}$")


def resolve(root: str, logical: str) -> str | None:
    """Return the current-generation file for ``logical`` under ``root``.

    Returns ``None`` on any error so callers can fall back to a raw path.
    Cached for ``_CACHE_TTL_SECONDS`` per (root, logical) — see module
    docstring note above ``_cache`` for why.
    """
    key = (root, logical)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    result = _resolve_uncached(root, logical)
    _cache[key] = (now, result)
    return result


def _resolve_uncached(root: str, logical: str) -> str | None:
    try:
        with open(os.path.join(root, "current.json"), encoding="utf-8") as fh:
            generation = json.load(fh).get("generation", "")
        if not isinstance(generation, str) or not _GENERATION_RE.match(generation):
            return None
        gen_dir = os.path.join(root, generation)
        with open(os.path.join(gen_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        if manifest.get("generation") != generation:
            return None
        for entry in manifest.get("entries", []):
            if entry.get("logical") != logical:
                continue
            file = entry.get("file", "")
            # Reject absolute paths or traversal; the file must stay inside gen_dir.
            if not file or os.path.isabs(file) or ".." in file.split(os.sep):
                return None
            path = os.path.join(gen_dir, file)
            return path if os.path.isfile(path) else None
    except (OSError, ValueError):
        return None
    return None


def snapshot_or_raw(raw_path: str) -> str:
    """Snapshot file for a known raw production path, else ``raw_path`` unchanged."""
    target = _RAW_TARGETS.get(raw_path)
    if target is None:
        return raw_path
    root, logical = target
    configured_root = (os.getenv(_ROOT_ENV.get(root, "")) or "").strip() or root
    resolved = resolve(configured_root, logical)
    return resolved if resolved else raw_path
