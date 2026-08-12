from __future__ import annotations

import duckdb
import pytest

from app.services.screener import _open_sandboxed_query_connection


def test_custom_screener_connection_cannot_reenable_external_access(tmp_path):
    output = tmp_path / "injected.csv"
    connection = _open_sandboxed_query_connection()
    try:
        with pytest.raises(duckdb.Error):
            connection.execute("SET enable_external_access = true")
        with pytest.raises(duckdb.Error):
            connection.execute(f"COPY (SELECT 1) TO '{output.as_posix()}' (FORMAT CSV)")
    finally:
        connection.close()

    assert not output.exists()
