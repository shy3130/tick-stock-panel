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


def test_extended_and_moneyflow_minute_have_independent_default_roots():
    # Contract A: these two logicals must resolve to dedicated snapshot roots,
    # not the shared fstore / engine-a generation roots. A revert to the shared
    # constant would make this fail.
    assert sr._RAW_TARGETS["/Volumes/WD1/duckdb/fstore-extended.duckdb"] == (
        sr.ROOT_FSTORE_EXTENDED,
        "extended",
    )
    assert sr.ROOT_FSTORE_EXTENDED != sr.ROOT_FSTORE
    assert sr._RAW_TARGETS["/Volumes/WD1/duckdb/tdx-moneyflow-minute.duckdb"] == (
        sr.ROOT_ENGINE_A_MONEYFLOW_MINUTE,
        "tdx_moneyflow_minute",
    )
    assert sr.ROOT_ENGINE_A_MONEYFLOW_MINUTE != sr.ROOT_ENGINE_A


def test_snapshot_or_raw_extended_prefers_independent_root(tmp_path, monkeypatch):
    # Publish the extended snapshot under the dedicated root and an older decoy
    # under the shared fstore root. The production raw path must resolve to the
    # dedicated root's snapshot — never the shared-root decoy — because the
    # default _RAW_TARGETS entry points at ROOT_FSTORE_EXTENDED (asserted above).
    indep = str(tmp_path / "fstore-extended")
    shared = str(tmp_path / "fstore")
    want = _publish(indep, "20260710T153000", "extended", "fstore-extended.duckdb")
    _publish(shared, "20260709T000000", "extended", "fstore-extended.duckdb")
    raw = "/Volumes/WD1/duckdb/fstore-extended.duckdb"
    monkeypatch.setitem(sr._RAW_TARGETS, raw, (indep, "extended"))
    assert sr.snapshot_or_raw(raw) == want


def test_snapshot_or_raw_extended_honours_dedicated_root_env(tmp_path, monkeypatch):
    root = str(tmp_path / "fstore-extended-staging")
    want = _publish(root, "20260710T153000", "extended", "fstore-extended.duckdb")
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED", root)

    assert sr.snapshot_or_raw("/Volumes/WD1/duckdb/fstore-extended.duckdb") == want


def test_snapshot_or_raw_moneyflow_minute_prefers_independent_root(tmp_path, monkeypatch):
    indep = str(tmp_path / "engine-a-moneyflow-minute")
    shared = str(tmp_path / "engine-a")
    want = _publish(
        indep, "20260710T153000", "tdx_moneyflow_minute", "tdx-moneyflow-minute.duckdb"
    )
    _publish(
        shared, "20260709T000000", "tdx_moneyflow_minute", "tdx-moneyflow-minute.duckdb"
    )
    raw = "/Volumes/WD1/duckdb/tdx-moneyflow-minute.duckdb"
    monkeypatch.setitem(sr._RAW_TARGETS, raw, (indep, "tdx_moneyflow_minute"))
    assert sr.snapshot_or_raw(raw) == want