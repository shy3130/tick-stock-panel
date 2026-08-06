"""FStoreDuckDBClient 测试。

某些测试（query 功能）需要本机挂载 /Volumes/WD1，否则自动 skip。
但安全网测试（fail-soft 行为）在任何环境都运行。
"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient

DUCKDB_PATH = "/Volumes/WD1/duckdb/fstore.duckdb"


@pytest.mark.skipif(
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


@pytest.mark.skipif(
    not os.path.exists(DUCKDB_PATH),
    reason=f"本机没有挂载 {DUCKDB_PATH}",
)
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



class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_setup_failure_closes_unpublished_connection(monkeypatch):
    """ATTACH/view 设置失败时，已打开但尚未发布的连接必须被关闭，避免泄漏。"""
    import app.data_providers.fquant.fstore_duckdb_client as fstore_mod

    opened: list[_FakeConn] = []

    def fake_connect(_path, *, read_only=False):
        conn = _FakeConn()
        opened.append(conn)
        return conn

    monkeypatch.setattr(fstore_mod.snapshot_or_raw, "__call__", lambda self, p: p)
    monkeypatch.setattr(
        "app.storage.duckdb_runtime.connect_duckdb", fake_connect
    )
    monkeypatch.setattr(os.path, "exists", lambda _p: True)
    client = FStoreDuckDBClient(path="/tmp/exists.duckdb")
    # _create_temp_views 之外的任一设置步骤抛错即覆盖整段设置 try。
    monkeypatch.setattr(client, "_create_temp_views", lambda conn: (_ for _ in ()).throw(RuntimeError("boom")))

    assert client._get_conn() is None
    assert client._available is False
    assert len(opened) == 1
    assert opened[0].closed is True


def test_close_is_idempotent_and_releases_connection():
    """close() 多次调用不抛、幂等；连接被关闭、_available 复位为 None。"""
    conn = _FakeConn()
    client = FStoreDuckDBClient(path="/tmp/whatever.duckdb")
    client._conn = conn
    client._available = True

    client.close()
    assert conn.closed is True
    assert client._conn is None
    assert client._available is None

    # 幂等：再次关闭不抛、不改变状态。
    client.close()
    assert client._conn is None
    assert client._available is None