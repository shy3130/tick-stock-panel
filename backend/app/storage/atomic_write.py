"""Shared atomic parquet write helper.

``df.write_parquet(out)`` straight to the final path leaves a truncated,
corrupt file if the process dies mid-write (crash, kill -9, disk full) — and
a single corrupt file under a glob-scanned directory breaks every DuckDB view
over it (``read_parquet`` raises ``InvalidInputException``, not
``duckdb.IOException``, on a truncated file — see the startup view
registration guard in ``app.storage.repository.DataStore._register_views``).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import polars as pl


def atomic_write_parquet(df: pl.DataFrame, out: str | Path) -> None:
    """Write ``df`` to ``out`` atomically via a same-directory temp file + rename.

    A crash mid-write leaves only an orphaned ``.tmp`` file, ignored by the
    ``*.parquet`` glob pattern; ``out`` stays whichever generation (old or
    new) fully completed.
    """
    out = Path(out)
    fd, tmp_name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        df.write_parquet(tmp_path)
        os.replace(tmp_path, out)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
