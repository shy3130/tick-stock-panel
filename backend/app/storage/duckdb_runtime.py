"""Shared DuckDB connection factory — the single source of runtime config.

Every DuckDB connection opened by the app (long-lived provider/store instances
and short-lived request-local scans alike) must go through :func:`connect_duckdb`
so that the memory-limit and thread budgets configured in ``Settings`` are
enforced uniformly. The values are overridable via the standard pydantic-settings
environment variables ``DUCKDB_MEMORY_LIMIT`` and ``DUCKDB_THREADS``.

No caller should use ``duckdb.connect`` directly; only this module does. There is
no fallback that silently drops the limits — invalid operator configuration fails
at exactly the same boundary where a direct ``duckdb.connect`` would fail.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from app.config import settings


def connect_duckdb(
    database: str | Path = ":memory:",
    *,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with the app-wide memory/thread budget.

    Mirrors ``duckdb.connect(database, read_only=...)`` but injects the global
    operator configuration derived from :class:`~app.config.Settings`. The
    ``threads`` value is stringified because DuckDB requires a textual config
    payload.
    """
    return duckdb.connect(
        str(database),
        read_only=read_only,
        config={
            "memory_limit": settings.duckdb_memory_limit,
            "threads": str(settings.duckdb_threads),
        },
    )
