"""Tests for atomic parquet writes and the startup view-registration guard.

Covers the fix for: a process crash mid-write used to leave a truncated
part.parquet, which then crashed the whole app at startup because
DuckDB's read_parquet raises InvalidInputException on a truncated file —
not duckdb.IOException, the only exception _register_views used to catch.
"""
from __future__ import annotations

import os

import duckdb
import polars as pl
import pytest

from app.storage.atomic_write import atomic_write_parquet as _atomic_write_parquet
from app.storage.repository import DataStore


def test_atomic_write_produces_readable_file(tmp_path):
    out = tmp_path / "part.parquet"
    df = pl.DataFrame({"symbol": ["a", "b"], "close": [1.0, 2.0]})
    _atomic_write_parquet(df, out)
    assert out.exists()
    assert pl.read_parquet(out).sort("symbol").to_dicts() == df.sort("symbol").to_dicts()


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path):
    out = tmp_path / "part.parquet"
    _atomic_write_parquet(pl.DataFrame({"a": [1]}), out)
    leftovers = [p for p in tmp_path.iterdir() if p != out]
    assert leftovers == []


def test_atomic_write_failure_does_not_corrupt_existing_file(tmp_path, monkeypatch):
    out = tmp_path / "part.parquet"
    good = pl.DataFrame({"symbol": ["a"], "close": [1.0]})
    _atomic_write_parquet(good, out)

    def boom(self, *a, **kw):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", boom)
    with pytest.raises(RuntimeError):
        _atomic_write_parquet(pl.DataFrame({"symbol": ["b"], "close": [2.0]}), out)

    # out must still be the last fully-written (old) generation, not corrupted.
    assert pl.read_parquet(out).to_dicts() == good.to_dicts()
    # and no orphaned .tmp file left in the directory.
    leftovers = [p for p in tmp_path.iterdir() if p != out]
    assert leftovers == []


def test_atomic_write_never_leaves_truncated_file_at_out_path(tmp_path, monkeypatch):
    """The failure mode this whole fix targets: out must never be partially written."""
    out = tmp_path / "part.parquet"

    def boom(self, *a, **kw):
        # Simulate dying mid-write: write_parquet itself never completes.
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", boom)
    with pytest.raises(RuntimeError):
        _atomic_write_parquet(pl.DataFrame({"a": [1]}), out)

    assert not out.exists()


def test_corrupt_partition_file_does_not_crash_datastore_startup(tmp_path):
    """Regression test for the startup crash: a truncated part.parquet under a
    glob-scanned directory used to raise an uncaught InvalidInputException
    from DataStore.__init__ -> _register_views, taking the whole app down.
    """
    part_dir = tmp_path / "kline_daily" / "date=2026-07-11"
    part_dir.mkdir(parents=True)
    (part_dir / "part.parquet").write_bytes(b"not a valid parquet file")

    # Must not raise.
    store = DataStore(data_dir=tmp_path)

    # The view is unavailable (registration was skipped), but the connection
    # and app are alive — querying the broken view fails gracefully instead
    # of the process never having started.
    with pytest.raises(duckdb.Error):
        store.db.execute("SELECT * FROM kline_daily").fetchall()
