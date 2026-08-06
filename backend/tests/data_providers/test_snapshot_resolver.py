"""Tests for the pure-Python snapshot resolver.

Mirrors engine pkg/snapshot semantics: known raw production paths resolve to the
current-generation file when a snapshot exists, and everything else (unknown
paths, ``-web`` paths, missing/malformed manifests) falls back to the raw path.
"""
from __future__ import annotations

import json
import os

from app.data_providers.fquant import snapshot_resolver as sr


def _publish(root: str, generation: str, logical: str, file: str) -> str:
    """Create a minimal valid snapshot layout and return the snapshot file path."""
    gen_dir = os.path.join(root, generation)
    os.makedirs(gen_dir, exist_ok=True)
    snap_file = os.path.join(gen_dir, file)
    with open(snap_file, "wb") as fh:
        fh.write(b"duckdb-bytes")
    with open(os.path.join(gen_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "generation": generation,
                "created_at": "2026-07-10T15:30:00Z",
                "entries": [{"logical": logical, "file": file, "size_bytes": 12}],
            },
            fh,
        )
    with open(os.path.join(root, "current.json"), "w", encoding="utf-8") as fh:
        json.dump({"generation": generation}, fh)
    return snap_file


def test_resolve_returns_current_generation_file(tmp_path):
    root = str(tmp_path / "engine-a")
    want = _publish(root, "20260710T153000", "tdx", "tdx.duckdb")
    assert sr.resolve(root, "tdx") == want


def test_resolve_missing_root_returns_none(tmp_path):
    assert sr.resolve(str(tmp_path / "nope"), "tdx") is None


def test_resolve_unknown_logical_returns_none(tmp_path):
    root = str(tmp_path / "engine-a")
    _publish(root, "20260710T153000", "tdx", "tdx.duckdb")
    assert sr.resolve(root, "not_published") is None


def test_resolve_bad_generation_returns_none(tmp_path):
    root = str(tmp_path / "engine-a")
    os.makedirs(root)
    with open(os.path.join(root, "current.json"), "w", encoding="utf-8") as fh:
        json.dump({"generation": "../escape"}, fh)
    assert sr.resolve(root, "tdx") is None


def test_snapshot_or_raw_unknown_path_is_raw():
    raw = "/Volumes/WD1/duckdb/fstore-web.duckdb"  # -web path, deliberately not mapped
    assert sr.snapshot_or_raw(raw) == raw


def test_snapshot_or_raw_known_path_without_snapshot_is_raw(tmp_path, monkeypatch):
    # Isolate the test from a real snapshot that may be published on this host.
    raw = "/Volumes/WD1/duckdb/tdx.duckdb"
    missing_root = str(tmp_path / "unpublished-engine-a")
    monkeypatch.setitem(sr._RAW_TARGETS, raw, (missing_root, "tdx"))
    assert sr.snapshot_or_raw(raw) == raw


def test_snapshot_or_raw_prefers_snapshot(tmp_path, monkeypatch):
    root = str(tmp_path / "engine-a")
    want = _publish(root, "20260710T153000", "tdx", "tdx.duckdb")
    monkeypatch.setitem(sr._RAW_TARGETS, "/Volumes/WD1/duckdb/tdx.duckdb", (root, "tdx"))
    assert sr.snapshot_or_raw("/Volumes/WD1/duckdb/tdx.duckdb") == want


def test_minutes_raw_paths_are_catalog_only_not_statically_resolved():
    # Date-sharded minutes logicals must resolve only through the published
    # catalog (catalog_resolver.resolve_route), never via a static
    # snapshot_or_raw bypass that skips date/generation validation.
    minutes_raw_paths = [
        "/Volumes/WD1/duckdb/tdx-minutes/tdx-minutes-before-2023.duckdb",
        "/Volumes/WD1/duckdb/tdx-minutes/tdx-minutes-from-2023.duckdb",
    ]
    for raw in minutes_raw_paths:
        assert raw not in sr._RAW_TARGETS
        assert sr.snapshot_or_raw(raw) == raw
