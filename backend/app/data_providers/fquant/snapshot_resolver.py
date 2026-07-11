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

# Canonical per-domain snapshot roots (must match engine pkg/snapshot).
ROOT_FSTORE = "/Volumes/WD1/snapshots/fstore"
ROOT_ENGINE_A = "/Volumes/WD1/snapshots/engine-a"
ROOT_ENGINE_HK = "/Volumes/WD1/snapshots/engine-hk"

# Raw production path -> (root, logical). Mirrors engine pkg/snapshot.rawTargets.
# Note: the panel's default ``-web`` paths are intentionally absent; they fall
# back to raw and are served from the separate web copies.
_RAW_TARGETS: dict[str, tuple[str, str]] = {
    "/Volumes/WD1/tdx.duckdb": (ROOT_ENGINE_A, "tdx"),
    "/Volumes/WD1/tdx-minutes.duckdb": (ROOT_ENGINE_A, "tdx_minutes"),
    "/Volumes/WD1/tdx-trans.duckdb": (ROOT_ENGINE_A, "tdx_trans"),
    "/Volumes/WD1/tdx-chip.duckdb": (ROOT_ENGINE_A, "tdx_chip"),
    "/Volumes/WD1/tdx-moneyflow-minute.duckdb": (ROOT_ENGINE_A, "tdx_moneyflow_minute"),
    "/Volumes/WD1/tdx-hk.duckdb": (ROOT_ENGINE_HK, "tdx_hk"),
    "/Volumes/WD1/tdx-hkminutes.duckdb": (ROOT_ENGINE_HK, "tdx_hk_minutes"),
    "/Volumes/WD1/tdx-hktrans.duckdb": (ROOT_ENGINE_HK, "tdx_hk_trans"),
    "/Volumes/WD1/fstore.duckdb": (ROOT_FSTORE, "fstore"),
    "/Volumes/WD1/fstore-markets.duckdb": (ROOT_FSTORE, "markets"),
    "/Volumes/WD1/fstore-klines.duckdb": (ROOT_FSTORE, "klines"),
    "/Volumes/WD1/fstore-minutes.duckdb": (ROOT_FSTORE, "minutes"),
}

_GENERATION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}$")


def resolve(root: str, logical: str) -> str | None:
    """Return the current-generation file for ``logical`` under ``root``.

    Returns ``None`` on any error so callers can fall back to a raw path.
    """
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
    resolved = resolve(target[0], target[1])
    return resolved if resolved else raw_path
