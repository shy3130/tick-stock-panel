"""FStoreDuckDBClient 测试 —— 需要本机挂载 /Volumes/WD1，否则自动 skip。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient

DUCKDB_PATH = "/Volumes/WD1/fstore.duckdb"

pytestmark = pytest.mark.skipif(
    not os.path.exists(DUCKDB_PATH),
    reason=f"本机没有挂载 {DUCKDB_PATH}",
)


def test_query_returns_list_of_dict():
    client = FStoreDuckDBClient()
    rows = client.query(
        "SELECT code, name FROM base_infos WHERE code = %s",
        ("600519",),
    )
    assert len(rows) == 1
    assert rows[0]["code"] == "600519"
    assert isinstance(rows[0], dict)


def test_query_with_in_clause_placeholders():
    client = FStoreDuckDBClient()
    rows = client.query(
        "SELECT code FROM base_infos WHERE asset_type IN (%s,%s) ORDER BY code LIMIT 5",
        (1, 10),
    )
    assert len(rows) > 0


def test_query_unknown_table_returns_empty_not_raise():
    client = FStoreDuckDBClient()
    rows = client.query("SELECT * FROM table_that_does_not_exist WHERE code = %s", ("x",))
    assert rows == []


def test_available_false_when_file_missing():
    client = FStoreDuckDBClient(path="/tmp/does-not-exist.duckdb")
    assert client.available is False
    assert client.query("SELECT 1") == []
