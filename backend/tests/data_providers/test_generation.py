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


def test_extended_root_is_independent_of_fstore(tmp_path, monkeypatch):
    # Contract A: extended honours its own env var and ignores the shared fstore
    # root entirely, even while FQUANT_SNAPSHOT_ROOT_FSTORE is set.
    indep = str(tmp_path / "fstore-extended")
    shared = str(tmp_path / "fstore")
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED", indep)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", shared)
    assert gen.root_for("extended") == indep
    # With the dedicated env unset it falls back to the dedicated default root,
    # NOT to the fstore env override still set above.
    monkeypatch.delenv("FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED", raising=False)
    assert gen.root_for("extended") == "/Volumes/WD1/duckdb/snapshots/fstore-extended"


def test_current_path_extended_prefers_independent_root(tmp_path, monkeypatch):
    indep = str(tmp_path / "fstore-extended")
    shared = str(tmp_path / "fstore")
    want = _publish(indep, "20260710T153000", "extended", "fstore-extended.duckdb")
    # Older decoy under the shared fstore root must be ignored.
    _publish(shared, "20260709T000000", "extended", "fstore-extended.duckdb")
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED", indep)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", shared)
    assert gen.current_path("extended") == want


def test_moneyflow_minute_root_is_independent_of_engine_a(tmp_path, monkeypatch):
    indep = str(tmp_path / "engine-a-moneyflow-minute")
    shared = str(tmp_path / "engine-a")
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE", indep)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", shared)
    assert gen.root_for("tdx_moneyflow_minute") == indep
    monkeypatch.delenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE", raising=False)
    assert (
        gen.root_for("tdx_moneyflow_minute")
        == "/Volumes/WD1/duckdb/snapshots/engine-a-moneyflow-minute"
    )


def test_current_path_moneyflow_minute_prefers_independent_root(tmp_path, monkeypatch):
    indep = str(tmp_path / "engine-a-moneyflow-minute")
    shared = str(tmp_path / "engine-a")
    want = _publish(
        indep, "20260710T153000", "tdx_moneyflow_minute", "tdx-moneyflow-minute.duckdb"
    )
    _publish(
        shared, "20260709T000000", "tdx_moneyflow_minute", "tdx-moneyflow-minute.duckdb"
    )
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE", indep)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", shared)
    assert gen.current_path("tdx_moneyflow_minute") == want