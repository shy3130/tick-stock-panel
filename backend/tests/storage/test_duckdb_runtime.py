"""connect_duckdb 工厂契约测试。

工厂是应用内 DuckDB 运行预算的唯一入口：精确 config 被测试，不依赖本机挂载的
真实库文件。通过对 ``duckdb.connect`` 取证（而非读取 DuckDB 归一化后的设置值）来
断言工厂注入的 config 与 ``Settings`` 完全一致。
"""
from __future__ import annotations

import pytest

import app.storage.duckdb_runtime as rt
from app.config import settings


@pytest.fixture
def captured_connect(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []
    sentinel = object()

    def fake_connect(database: str, *, read_only: bool = False, config=None):
        calls.append(
            {"database": database, "read_only": read_only, "config": dict(config or {})}
        )
        return sentinel

    monkeypatch.setattr(rt.duckdb, "connect", fake_connect)
    return calls, sentinel


def test_default_in_memory_passes_exact_config(captured_connect):
    calls, sentinel = captured_connect
    con = rt.connect_duckdb()

    assert con is sentinel
    assert len(calls) == 1
    call = calls[0]
    assert call["database"] == ":memory:"
    assert call["read_only"] is False
    assert call["config"] == {
        "memory_limit": settings.duckdb_memory_limit,
        "threads": str(settings.duckdb_threads),
    }


def test_database_and_read_only_passthrough(captured_connect):
    calls, sentinel = captured_connect
    con = rt.connect_duckdb("/some/path.duckdb", read_only=True)

    assert con is sentinel
    assert calls[0]["database"] == "/some/path.duckdb"
    assert calls[0]["read_only"] is True
    assert calls[0]["config"] == {
        "memory_limit": settings.duckdb_memory_limit,
        "threads": str(settings.duckdb_threads),
    }


def test_path_object_stringified(captured_connect):
    calls, _ = captured_connect
    from pathlib import Path

    rt.connect_duckdb(Path("/x/y.duckdb"))
    assert calls[0]["database"] == "/x/y.duckdb"


def test_threads_value_is_stringified(monkeypatch: pytest.MonkeyPatch):
    """DuckDB 要求 config 全部为字符串：threads 必须被 str() 化。"""
    seen: dict = {}
    monkeypatch.setattr(settings, "duckdb_threads", 8)
    monkeypatch.setattr(settings, "duckdb_memory_limit", "1GB")

    def fake_connect(database: str, *, read_only: bool = False, config=None):
        seen["config"] = config
        return object()

    monkeypatch.setattr(rt.duckdb, "connect", fake_connect)
    rt.connect_duckdb()
    assert seen["config"] == {"memory_limit": "1GB", "threads": "8"}
    assert isinstance(seen["config"]["threads"], str)


def test_no_silent_fallback_drops_limits(monkeypatch: pytest.MonkeyPatch):
    """无效 operator 配置必须在同一边界抛出，工厂不得吞掉限制作降级。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("Invalid Error: 'threads' requires a value >= 1")

    monkeypatch.setattr(rt.duckdb, "connect", boom)
    with pytest.raises(RuntimeError, match="threads"):
        rt.connect_duckdb()


def test_data_store_close_is_idempotent(tmp_path):
    from app.storage.repository import DataStore

    store = DataStore(tmp_path)
    store.close()
    store.close()

    assert store.db is None
