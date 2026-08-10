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

    monkeypatch.setattr(fstore_mod, "snapshot_or_raw", lambda path: path)
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


class _ExecuteConn(_FakeConn):
    """_FakeConn 的扩展：支持 execute/fetchall/description，用于 query() 断言。"""

    def __init__(self) -> None:
        super().__init__()
        self.execute_calls: list[tuple] = []
        self.fail_after = None  # 第 N 次 execute 抛错（None=永不）

    def execute(self, sql, params=None):
        self.execute_calls.append((sql, params))
        if self.fail_after is not None and len(self.execute_calls) >= self.fail_after:
            raise RuntimeError("query boom")
        return self  # cursor

    @property
    def description(self):
        return [("c",)]

    def fetchall(self):
        return []


def _patch_client_env(monkeypatch, connects):
    """让 FStoreDuckDBClient 视所有路径存在、走注入的 connect_duckdb。"""
    import app.data_providers.fquant.fstore_duckdb_client as fstore_mod

    monkeypatch.setattr(fstore_mod, "snapshot_or_raw", lambda path: path)
    monkeypatch.setattr(
        "app.storage.duckdb_runtime.connect_duckdb",
        lambda path, *, read_only=False: connects(path),
    )
    monkeypatch.setattr(os.path, "exists", lambda _p: True)


def test_initial_connect_failure_backoff_then_recover(monkeypatch):
    """首次连接失败：进入固定退避窗口（窗口内 query 返回空、不重连）；
    窗口过后下一次调用重新连接并恢复正常。"""
    import app.data_providers.fquant.fstore_duckdb_client as fstore_mod

    attempts = {"n": 0}
    recovered = _ExecuteConn()

    def connects(_path):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("disk gone")
        return recovered

    _patch_client_env(monkeypatch, connects)
    monkeypatch.setattr(fstore_mod, "RECONNECT_BACKOFF_SECONDS", 10.0)
    client = FStoreDuckDBClient(path="/tmp/backoff.duckdb")

    # 首次：连接抛错 → 不可用，进入退避窗口。
    assert client._get_conn() is None
    assert client._available is False
    assert client._unavailable_until is not None
    assert attempts["n"] == 1

    # 退避窗口内：query 返回空，且不重连（connect_duckdb 未被再次调用）。
    assert client.query("SELECT 1") == []
    assert attempts["n"] == 1

    # 模拟退避窗口已过：下次 query 重连成功。
    client._unavailable_until = fstore_mod.time.monotonic() - 1
    rows = client.query("SELECT 1")
    assert rows == []
    assert attempts["n"] == 2
    assert client._available is True
    assert client._unavailable_until is None
    assert client._conn is recovered


def test_query_exception_releases_connection_allows_reconnect(monkeypatch):
    """查询异常：连接被释放（关闭 + 复位），下次调用立即重连（不施加退避）。"""

    connects: list[_ExecuteConn] = []

    def make_conn(_path):
        conn = _ExecuteConn()
        connects.append(conn)
        return conn

    _patch_client_env(monkeypatch, make_conn)
    client = FStoreDuckDBClient(path="/tmp/qerr.duckdb")

    # 建立第一连接，使其下一次 execute 抛错。
    first = client._get_conn()
    assert first is connects[0]
    first.fail_after = 1

    # 查询抛错 → 返回空，连接被关闭并释放，无退避窗口。
    assert client.query("SELECT 1") == []
    assert connects[0].closed is True
    assert client._conn is None
    assert client._available is None
    assert client._unavailable_until is None

    # 下一次调用立即重连（建立新连接），不受退避阻挡。
    second = client._get_conn()
    assert second is connects[1]
    assert second is not connects[0]
    assert client._available is True


def test_close_clears_backoff_window(monkeypatch):
    """close() 复位退避状态：之后可立即重连，不会被遗留的 _unavailable_until 阻挡。"""
    client = FStoreDuckDBClient(path="/tmp/clear.duckdb")
    client._available = False
    client._unavailable_until = float("inf")  # 永久阻挡的极端值

    client.close()
    assert client._available is None
    assert client._unavailable_until is None