"""筹码分布（stock_chip_peaks）provider 契约测试。

覆盖：
- TdxDuckDBClient.get_chip: 标的前缀、日期范围 WHERE、LIMIT、字段覆盖
- TdxDuckDBClient.get_chip_coverage: 不可达与空表 reason 区分
- strict snapshot: 未发布快照时绝不回退 raw（available=False，不是读 raw）
- FQuantProvider.get_chip_distribution: symbol 正则校验、limit、字段映射、source/provenance
- FQuantProvider.get_chip_status: 结构与 key
- 只读语义: ConnectionSet opener 始终 read_only=True

不依赖本机 DuckDB 挂载——用 fake 连接 / monkeypatch generation。
"""
from __future__ import annotations

import datetime
import os
import sys

import polars as pl
import pytest

from app.data_providers.fquant import generation as gen
from app.data_providers.fquant.tdx_duckdb_client import TdxDuckDBClient


# --------------------------------------------------------------------------- #
# Fake connections
# --------------------------------------------------------------------------- #

class _FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description

    def fetchall(self):
        return self._rows


class _FakeQueryConn:
    """Records the last execute(sql, params) and returns canned rows."""

    def __init__(self, rows=None, description=None, path=""):
        self.last_sql = ""
        self.last_params = []
        self.path = path
        self._rows = rows or []
        self._desc = description or []
        self.closed = False

    def cursor(self):
        return self

    def execute(self, sql, params):
        self.last_sql = sql
        self.last_params = list(params)
        return _FakeCursor(self._rows, self._desc)

    def close(self):
        self.closed = True


class _FakeDictConn:
    """For query_dicts: returns list[dict] keyed by column names."""

    def __init__(self, dict_rows, path=""):
        self.last_sql = ""
        self.last_params = []
        self.path = path
        self._dict_rows = dict_rows
        self.closed = False

    def cursor(self):
        return self

    def execute(self, sql, params):
        self.last_sql = sql
        self.last_params = list(params)
        # Derive column names from first dict key order
        if self._dict_rows:
            names = list(self._dict_rows[0].keys())
            tuples = [tuple(r[n] for n in names) for r in self._dict_rows]
            return _FakeCursor(tuples, [(n,) for n in names])
        return _FakeCursor([], [])

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# TdxDuckDBClient.get_chip — symbol prefix, date range, limit, fields
# --------------------------------------------------------------------------- #

def _make_client_with_chip_rows(dict_rows):
    """Build a TdxDuckDBClient whose _chip source returns ``dict_rows``."""
    from contextlib import contextmanager

    from app.data_providers.fquant.lease import ConnectionSet

    client = TdxDuckDBClient()
    conn = _FakeDictConn(dict_rows, path="/fake/snapshot/tdx_chip.duckdb")

    # Build a real ConnectionSet whose opener always returns our fake conn.
    client._chip._set = ConnectionSet(lambda p: conn)
    # Bypass generation resolution: always resolve to the fake path.
    client._chip._resolve = lambda: "/fake/snapshot/tdx_chip.duckdb"
    # Return the existing set (already built) from _ensure_set.
    client._chip._ensure_set = lambda: client._chip._set
    return client, conn


def test_get_chip_prefixes_code_and_filters_by_date_and_limit(monkeypatch):
    """get_chip must prefix the code, apply date WHERE clauses, ORDER ASC, and LIMIT."""
    sample = {
        "trade_date": datetime.date(2026, 8, 7),
        "peak_price": 1424.0,
        "peak_volume": 1000.0,
        "peak_ratio": 0.015,
        "profit_ratio": 0.207,
        "avg_cost": 1398.83,
        "concentration_90": 12.46,
        "range_90_low": 1208.0,
        "range_90_high": 1552.0,
        "concentration_70": 7.43,
        "range_70_low": 1208.0,
        "range_70_high": 1552.0,
        "cr10": 2.45,
        "cr30": 7.98,
        "gini": 0.569,
        "main_peak_price": 1424.0,
        "main_peak_volume": 1000.0,
        "main_peak_ratio": 0.015,
        "main_concentration": 0.357,
        "retail_peak_price": None,
        "retail_peak_volume": None,
        "retail_peak_ratio": None,
        "retail_concentration": 0.405,
        "has_retail_peak": False,
        "peak_count": 5,
        "window_days": 360,
        "price_step": 2.0,
        "asset_type": 1,
    }
    client, conn = _make_client_with_chip_rows([sample])

    rows = client.get_chip(
        "sh600519", "2026-01-01", "2026-08-10", limit=50,
    )
    assert len(rows) == 1
    # Verify SQL shape: code filter, date range, ascending order, limit
    sql = conn.last_sql
    assert "code = ?" in sql
    assert "trade_date >= ?" in sql
    assert "trade_date <= ?" in sql
    assert "ORDER BY trade_date ASC" in sql
    assert "LIMIT ?" in sql
    # params: [code, start, end, limit]
    assert conn.last_params == ["sh600519", "2026-01-01", "2026-08-10", 50]
    client.close()


def test_get_chip_without_date_bounds_omits_where_clauses(monkeypatch):
    """When start/end are None, no date filter should be added."""
    client, conn = _make_client_with_chip_rows([])
    client.get_chip("sz000001", None, None, limit=100)
    sql = conn.last_sql
    assert "trade_date >=" not in sql
    assert "trade_date <=" not in sql
    assert conn.last_params == ["sz000001", 100]
    client.close()


def test_get_chip_covers_all_expected_fields():
    """Every contract field must be present in the returned dict."""
    sample = {
        "trade_date": datetime.date(2026, 8, 7),
        "peak_price": 1424.0, "peak_volume": 1000.0, "peak_ratio": 0.015,
        "profit_ratio": 0.207, "avg_cost": 1398.83,
        "concentration_90": 12.46, "range_90_low": 1208.0, "range_90_high": 1552.0,
        "concentration_70": 7.43, "range_70_low": 1208.0, "range_70_high": 1552.0,
        "cr10": 2.45, "cr30": 7.98, "gini": 0.569,
        "main_peak_price": 1424.0, "main_peak_volume": 1000.0, "main_peak_ratio": 0.015,
        "main_concentration": 0.357,
        "retail_peak_price": None, "retail_peak_volume": None, "retail_peak_ratio": None,
        "retail_concentration": 0.405, "has_retail_peak": False,
        "peak_count": 5, "window_days": 360, "price_step": 2.0, "asset_type": 1,
    }
    expected_keys = set(sample.keys())
    client, _ = _make_client_with_chip_rows([sample])
    rows = client.get_chip("sh600519", None, None)
    assert set(rows[0].keys()) == expected_keys
    client.close()


def test_get_chip_snapshot_uses_exact_date_and_a_share_rows_only():
    sample = {
        "code": "sh600519",
        "trade_date": datetime.date(2026, 8, 14),
        "profit_ratio": 0.207,
        "avg_cost": 1398.83,
        "concentration_90": 12.46,
        "peak_count": 5,
        "main_peak_price": 1424.0,
    }
    client, conn = _make_client_with_chip_rows([sample])

    rows = client.get_chip_snapshot("2026-08-14")

    assert rows == [sample]
    assert "WHERE trade_date = ? AND asset_type = 1" in conn.last_sql
    assert conn.last_params == ["2026-08-14"]
    client.close()


# --------------------------------------------------------------------------- #
# get_chip_coverage — unavailable vs empty vs populated
# --------------------------------------------------------------------------- #

def test_get_chip_coverage_populated(monkeypatch):
    """When snapshot has data, available=True with min/max/count/symbols."""
    client, _ = _make_client_with_chip_rows([{
        "rows": 100, "earliest_date": "2024-09-06",
        "latest_date": "2026-08-10", "symbols": 5547,
    }])
    result = client.get_chip_coverage()
    assert result["available"] is True
    assert result["source"] == "tdx_chip"
    assert result["earliest_date"] == "2024-09-06"
    assert result["latest_date"] == "2026-08-10"
    assert result["rows"] == 100
    assert result["symbols"] == 5547
    assert result["reason"] is None
    client.close()


def test_get_chip_coverage_empty_table(monkeypatch):
    """When the table exists but has zero rows, reason='empty'."""
    client, _ = _make_client_with_chip_rows([{
        "rows": 0, "earliest_date": None,
        "latest_date": None, "symbols": 0,
    }])
    result = client.get_chip_coverage()
    assert result["available"] is False
    assert result["reason"] == "empty"
    assert result["rows"] == 0
    client.close()


def test_get_chip_coverage_snapshot_unavailable(monkeypatch):
    """When the snapshot is not published (strict, no raw fallback),
    query_dicts returns [] and reason='snapshot unavailable'."""
    client = TdxDuckDBClient()
    # Force _chip.lease() to yield None (snapshot unavailable)
    client._chip._resolve = lambda: None
    result = client.get_chip_coverage()
    assert result["available"] is False
    assert result["reason"] == "snapshot unavailable"
    assert result["rows"] == 0
    client.close()


# --------------------------------------------------------------------------- #
# strict snapshot — never falls back to raw
# --------------------------------------------------------------------------- #

def test_chip_source_is_strict_snapshot():
    """_chip must be configured with strict_snapshot=True (no raw fallback)."""
    client = TdxDuckDBClient()
    assert client._chip._strict_snapshot is True
    client.close()


def test_chip_source_resolves_none_when_unpublished(monkeypatch, tmp_path):
    """When generation has no current.json, _resolve returns None — not the raw path."""
    from app.data_providers.fquant import snapshot_resolver as sr

    sr._cache.clear()
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(tmp_path / "nonexistent"))
    client = TdxDuckDBClient(chip_path="/fake/raw/tdx-chip.duckdb")
    # Even though raw_path exists in config, strict mode must not return it.
    resolved = client._chip._resolve()
    assert resolved is None
    client.close()


def test_chip_source_resolves_published_snapshot(monkeypatch, tmp_path):
    """When a generation IS published, _resolve returns the snapshot file path."""
    import json as _json

    from app.data_providers.fquant import snapshot_resolver as sr

    root = str(tmp_path / "engine-a")
    gen_id = "20260810T120000"
    gen_dir = os.path.join(root, gen_id)
    os.makedirs(gen_dir)
    snap_file = os.path.join(gen_dir, "tdx_chip.duckdb")
    with open(snap_file, "wb") as f:
        f.write(b"dummy")
    with open(os.path.join(gen_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        _json.dump(
            {"generation": gen_id, "created_at": "2026-08-10T12:00:00Z",
             "entries": [{"logical": "tdx_chip", "file": "tdx_chip.duckdb",
                          "size_bytes": 5}]},
            fh,
        )
    with open(os.path.join(root, "current.json"), "w", encoding="utf-8") as fh:
        _json.dump({"generation": gen_id}, fh)

    # Clear resolver cache so the fresh publish is picked up.
    sr._cache.clear()
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", root)
    client = TdxDuckDBClient(chip_path="/fake/raw/tdx-chip.duckdb")
    resolved = client._chip._resolve()
    assert resolved == snap_file
    client.close()


# --------------------------------------------------------------------------- #
# read-only semantics
# --------------------------------------------------------------------------- #

def test_chip_connection_set_opener_is_read_only(monkeypatch):
    """The ConnectionSet opener must call connect_duckdb(path, read_only=True)."""
    import app.data_providers.fquant.lease as lease_mod

    seen = []

    class _FakeConn:
        def cursor(self):
            return self

        def execute(self, *_a, **_kw):
            return self

        def fetchall(self):
            return []

        def close(self):
            pass

    original_connect = None

    def fake_connect(path, read_only=False):
        seen.append((path, read_only))
        return _FakeConn()

    # Monkeypatch the import target module
    import types
    fake_mod = types.ModuleType("app.storage.duckdb_runtime")
    fake_mod.connect_duckdb = fake_connect
    monkeypatch.setitem(sys.modules, "app.storage.duckdb_runtime", fake_mod)

    client = TdxDuckDBClient(chip_path="/fake/tdx-chip.duckdb")
    # Force generation to resolve to a fake path
    monkeypatch.setattr(client._chip, "_resolve", lambda: "/fake/tdx-chip.duckdb")
    client._chip.query_dicts("SELECT 1", [], "test")
    client.close()

    assert seen, "connect_duckdb must have been called"
    assert seen[-1] == ("/fake/tdx-chip.duckdb", True), (
        "chip source must open the connection read_only=True"
    )


# --------------------------------------------------------------------------- #
# FQuantProvider.get_chip_distribution — symbol validation, fields, source
# --------------------------------------------------------------------------- #

def test_get_chip_distribution_rejects_non_a_share_symbol():
    """Only ^\\d{6}\\.(SH|SZ|BJ)$ is accepted; everything else returns empty."""
    from app.data_providers.fquant_provider import FQuantProvider

    provider = FQuantProvider()
    for bad in ["600519", "600519.HK", "00700.HK", "000001.INDEX", "", "ABC.SH"]:
        df = provider.get_chip_distribution(bad)
        assert df.is_empty(), f"symbol {bad!r} must be rejected"
    provider.close()


def test_get_chip_distribution_returns_correct_fields_and_source(monkeypatch):
    """get_chip_distribution maps all fields, adds symbol + source, omits result_json."""
    from app.data_providers.fquant_provider import FQuantProvider

    sample_row = {
        "trade_date": datetime.date(2026, 8, 7),
        "peak_price": 1424.0, "peak_volume": 1000.0, "peak_ratio": 0.015,
        "profit_ratio": 0.207, "avg_cost": 1398.83,
        "concentration_90": 12.46, "range_90_low": 1208.0, "range_90_high": 1552.0,
        "concentration_70": 7.43, "range_70_low": 1208.0, "range_70_high": 1552.0,
        "cr10": 2.45, "cr30": 7.98, "gini": 0.569,
        "main_peak_price": 1424.0, "main_peak_volume": 1000.0, "main_peak_ratio": 0.015,
        "main_concentration": 0.357,
        "retail_peak_price": None, "retail_peak_volume": None, "retail_peak_ratio": None,
        "retail_concentration": 0.405, "has_retail_peak": False,
        "peak_count": 5, "window_days": 360, "price_step": 2.0, "asset_type": 1,
    }
    provider = FQuantProvider()
    captured = {}

    def fake_get_chip(code, start_iso, end_iso, limit=500):
        captured["code"] = code
        captured["start"] = start_iso
        captured["end"] = end_iso
        captured["limit"] = limit
        return [sample_row]

    monkeypatch.setattr(provider._engine, "get_chip", fake_get_chip)

    df = provider.get_chip_distribution(
        "600519.SH",
        start=datetime.datetime(2026, 1, 1),
        end=datetime.datetime(2026, 8, 10),
        limit=50,
    )
    assert not df.is_empty()
    # Prefixed code passed to engine
    assert captured["code"] == "sh600519"
    assert captured["start"] == "2026-01-01"
    assert captured["end"] == "2026-08-10"
    assert captured["limit"] == 50

    row = df.row(0, named=True)
    assert row["symbol"] == "600519.SH"
    assert row["trade_date"] == "2026-08-07"
    assert row["peak_price"] == 1424.0
    assert row["avg_cost"] == 1398.83
    assert row["profit_ratio"] == 0.207
    assert row["gini"] == pytest.approx(0.569)
    assert row["source"] == "fquant:tdx_chip"
    # result_json must NOT be a column
    assert "result_json" not in df.columns
    # updated_at / source_version should not leak either
    assert "updated_at" not in df.columns
    assert "source_version" not in df.columns
    provider.close()


def test_get_chip_distribution_bj_symbol_gets_bj_prefix(monkeypatch):
    """BJ symbols (430xxx / 830xxx) must get the 'bj' prefix."""
    from app.data_providers.fquant_provider import FQuantProvider

    provider = FQuantProvider()
    captured = {}

    def fake_get_chip(code, start_iso, end_iso, limit=500):
        captured["code"] = code
        return []

    monkeypatch.setattr(provider._engine, "get_chip", fake_get_chip)
    provider.get_chip_distribution("430047.BJ")
    assert captured["code"] == "bj430047"
    provider.close()


def test_get_chip_distribution_empty_when_snapshot_unavailable(monkeypatch):
    """When the engine returns [] (snapshot unreachable), provider returns empty df."""
    from app.data_providers.fquant_provider import FQuantProvider

    provider = FQuantProvider()
    monkeypatch.setattr(provider._engine, "get_chip", lambda *a, **kw: [])
    df = provider.get_chip_distribution("600519.SH")
    assert df.is_empty()
    provider.close()


# --------------------------------------------------------------------------- #
# FQuantProvider.get_chip_status
# --------------------------------------------------------------------------- #

def test_get_chip_status_structure(monkeypatch):
    """get_chip_status returns {'chip': {available, source, ...}}."""
    from app.data_providers.fquant_provider import FQuantProvider

    provider = FQuantProvider()
    monkeypatch.setattr(
        provider._engine, "get_chip_coverage",
        lambda: {
            "available": True, "source": "tdx_chip",
            "earliest_date": "2024-09-06", "latest_date": "2026-08-10",
            "rows": 100, "symbols": 5547, "reason": None,
        },
    )
    result = provider.get_chip_status()
    assert "chip" in result
    fact = result["chip"]
    assert fact["available"] is True
    assert fact["source"] == "tdx_chip"
    assert fact["rows"] == 100
    assert fact["symbols"] == 5547
    provider.close()


def test_get_chip_status_independent_key():
    """get_chip_status must use the 'chip' key, not collide with moneyflow."""
    from app.data_providers.fquant_provider import FQuantProvider

    provider = FQuantProvider()
    result = provider.get_chip_status()
    assert set(result.keys()) == {"chip"}
    # moneyflow keys must not be present
    for mf_key in ("moneyflow_daily_stock", "moneyflow_daily_block",
                   "moneyflow_minute_stock", "moneyflow_minute_block"):
        assert mf_key not in result
    provider.close()
