"""Tests for logical -> snapshot-root resolution with env overrides."""
from __future__ import annotations

import json
import os

from app.data_providers.fquant import generation as gen


def _publish(root: str, generation: str, logical: str, file: str) -> str:
    gen_dir = os.path.join(root, generation)
    os.makedirs(gen_dir, exist_ok=True)
    snap = os.path.join(gen_dir, file)
    with open(snap, "wb") as fh:
        fh.write(b"bytes")
    with open(os.path.join(gen_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"generation": generation, "created_at": "2026-07-10T15:30:00Z",
             "entries": [{"logical": logical, "file": file, "size_bytes": 5}]},
            fh,
        )
    with open(os.path.join(root, "current.json"), "w", encoding="utf-8") as fh:
        json.dump({"generation": generation}, fh)
    return snap


def test_root_for_unknown_logical_is_none():
    assert gen.root_for("nope") is None


def test_date_sharded_logicals_are_resolved_only_by_catalog():
    assert gen.root_for("tdx_minutes_before_2023") is None
    assert gen.root_for("tdx_minutes_from_2023") is None
    assert gen.root_for("tdx_trans_2026") is None


def test_root_for_honours_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(tmp_path))
    assert gen.root_for("tdx") == str(tmp_path)
    # default when unset
    monkeypatch.delenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", raising=False)
    assert gen.root_for("tdx") == "/Volumes/WD1/duckdb/snapshots/engine-a"


def test_current_path_resolves_published(tmp_path, monkeypatch):
    root = str(tmp_path / "engine-a")
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", root)
    want = _publish(root, "20260710T153000", "tdx", "tdx.duckdb")
    assert gen.current_path("tdx") == want


def test_current_path_none_when_unpublished(tmp_path, monkeypatch):
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(tmp_path / "empty"))
    assert gen.current_path("tdx") is None
