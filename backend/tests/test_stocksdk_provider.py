"""StockSDKProvider 归一化与桥接契约测试。

不依赖真实 node / 网络: mock bridge.run_job 返回样例 payload, 只验证 Python 侧的
归一化、除权因子合成对齐、符号回显、空结果处理与注册接线。
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess

import polars as pl

from app.plugins.stocksdk import bridge
from app.plugins.stocksdk import provider as sp
from app.plugins.stocksdk.provider import StockSDKProvider


def _patch_run_job(monkeypatch, mapping):
    """mapping: op -> payload dict(将作为 run_job 返回值)。"""

    def fake(job, timeout=None):
        return mapping[job["op"]]

    monkeypatch.setattr(sp.bridge, "run_job", fake)


def test_get_daily_normalizes_and_echoes_symbol(monkeypatch):
    _patch_run_job(monkeypatch, {
        "daily": {"ok": True, "op": "daily", "rows": {
            "600519.SH": [
                {"date": "2026-01-05", "open": 1385.0, "high": 1431.9, "low": 1385.0,
                 "close": 1426.0, "volume": 70949, "amount": 1.0e10, "code": "600519"},
                {"date": "2026-01-06", "open": 1432.5, "high": 1437.0, "low": 1416.5,
                 "close": 1428.0, "volume": 39586, "amount": 5.6e9, "code": "600519"},
            ],
        }},
    })
    df = StockSDKProvider().get_daily(["600519.SH"], dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 15))
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df.height == 2
    assert df["symbol"].unique().to_list() == ["600519.SH"]
    assert df.schema["date"] == pl.Date
    assert df.schema["close"] == pl.Float64


def test_get_adj_factors_from_bridge_ratio(monkeypatch):
    # 桥接内部已算好 ex_factor = close_hfq/close_none, 这里验证 Python 侧归一化。
    _patch_run_job(monkeypatch, {
        "adj": {"ok": True, "op": "adj", "rows": {
            "600519.SH": [
                {"symbol": "600519.SH", "trade_date": "2020-01-02", "ex_factor": 5.29},
                {"symbol": "600519.SH", "trade_date": "2020-01-03", "ex_factor": 5.30},
            ],
        }},
    })
    df = StockSDKProvider().get_adj_factors(["600519.SH"], None, None)
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df.height == 2
    assert df.schema["trade_date"] == pl.Date
    assert abs(df["ex_factor"][0] - 5.29) < 1e-9


def test_get_minute_datetime_is_beijing_wall_clock(monkeypatch):
    # timestamp 1779327300000 = 2026-05-21 01:35 UTC = 09:35 Asia/Shanghai
    _patch_run_job(monkeypatch, {
        "minute": {"ok": True, "op": "minute", "rows": {
            "600519.SH": [
                {"date": "2026-05-21 09:35", "open": 1284.9, "high": 1289.1, "low": 1283.9,
                 "close": 1286.7, "volume": 2740, "amount": 3.6e8, "timestamp": 1779327300000},
            ],
        }},
    })
    df = StockSDKProvider().get_minute(["600519.SH"], None, None)
    assert set(df.columns) == {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
    assert df.height == 1
    ts = df["datetime"][0]
    assert (ts.hour, ts.minute) == (9, 35)
    assert df["symbol"][0] == "600519.SH"


def test_get_realtime_passthrough(monkeypatch):
    rows = [{"symbol": "600519.SH", "name": "贵州茅台", "last_price": 1200.0,
             "prev_close": 1194.0, "open": 1186.0, "high": 1203.0, "low": 1180.0, "volume": 16325}]
    _patch_run_job(monkeypatch, {"realtime": {"ok": True, "op": "realtime", "rows": rows}})
    out = StockSDKProvider().get_realtime()
    assert out == rows
    required = {"symbol", "last_price", "prev_close", "open", "high", "low", "volume"}
    assert required <= set(out[0].keys())


def test_get_instruments_flatten_compatible(monkeypatch):
    rows = [{"symbol": "600519.SH", "name": "贵州茅台", "code": "600519", "exchange": "SH",
             "region": "CN", "type": "stock", "total_shares": 1, "float_shares": 1,
             "limit_up": 1.0, "limit_down": 1.0}]
    _patch_run_job(monkeypatch, {"instruments": {"ok": True, "op": "instruments", "rows": rows}})
    out = StockSDKProvider().get_instruments("stock")
    assert out[0]["symbol"] == "600519.SH"
    assert out[0]["exchange"] == "SH"
    # 非 stock 资产暂不覆盖
    assert StockSDKProvider().get_instruments("etf") == []


def test_empty_symbols_returns_empty():
    p = StockSDKProvider()
    assert p.get_daily([], None, None).is_empty()
    assert p.get_adj_factors([], None, None).is_empty()
    assert p.get_minute([], None, None).is_empty()


def test_bridge_error_degrades_to_empty(monkeypatch):
    def boom(job, timeout=None):
        raise sp.bridge.StockSDKBridgeError("node missing")

    monkeypatch.setattr(sp.bridge, "run_job", boom)
    assert StockSDKProvider().get_daily(["600519.SH"], None, None).is_empty()
    assert StockSDKProvider().get_realtime() == []
    assert StockSDKProvider().get_instruments("stock") == []


def test_bridge_uses_utf8_error_tolerant_subprocess(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps({"ok": True, "op": "ping"})
        stderr = ""

    monkeypatch.setattr(bridge, "_node_bin", lambda: "node")

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert bridge.run_job({"op": "ping"})["ok"] is True
    kwargs = calls[0][1]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_bridge_mjs_resolves_local_stock_sdk_on_windows_path(tmp_path):
    if shutil.which("node") is None:
        raise AssertionError("node is required for stock-sdk bridge path regression test")

    bridge_path = tmp_path / "bridge.mjs"
    shutil.copyfile(bridge._BRIDGE_MJS, bridge_path)

    pkg_dir = tmp_path / "node_modules" / "stock-sdk"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": "stock-sdk", "type": "module", "main": "index.js"}),
        encoding="utf-8",
    )
    (pkg_dir / "index.js").write_text(
        "export class StockSDK { static version = 'fake-local' }\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({"op": "ping"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result == {"ok": True, "op": "ping", "version": "fake-local"}


def test_plugin_discovered_in_loader():
    """插件被发现并记录状态 (即使依赖没装, 不可用)。"""
    from app.data_providers import custom as cs

    plugins = {p["name"]: p for p in cs.list_plugins()}
    assert "stocksdk" in plugins
    assert plugins["stocksdk"]["runtime"] == "node"
    assert "daily" in plugins["stocksdk"]["datasets"]
    assert "realtime" in plugins["stocksdk"]["datasets"]
    assert "financial" not in plugins["stocksdk"]["datasets"]
    assert cs.is_builtin("stocksdk")
    # 内置源不出现在用户自定义源列表
    assert "stocksdk" not in [s["name"] for s in cs.list_sources()]


def test_plugin_registered_when_available(monkeypatch):
    """依赖可用时, 插件注册进 _PROVIDERS 并可路由。"""
    from app.data_providers import custom as cs
    from app.data_providers.custom import loader

    # mock availability 返回 (True, "ok")
    monkeypatch.setattr(loader, "_call_check", lambda ref: (True, "ok"))
    monkeypatch.setattr(loader, "_load_entry", _load_stocksdk_entry)
    loader._load_builtin_plugins()

    assert "stocksdk" in cs.names()
    assert cs.is_custom_provider("stocksdk")
    assert cs.provider_has_dataset("stocksdk", "daily")
    assert cs.provider_has_dataset("stocksdk", "realtime")
    assert not cs.provider_has_dataset("stocksdk", "financial")


def _load_stocksdk_entry(entry_ref: str):
    """测试用: 无条件加载 stocksdk provider 类 (跳过 check)。"""
    if "StockSDKProvider" in entry_ref:
        from app.plugins.stocksdk.provider import StockSDKProvider
        return StockSDKProvider
    if "availability" in entry_ref:
        from app.plugins.stocksdk.bridge import availability
        return availability
    raise ValueError(f"unknown entry: {entry_ref}")


def test_builtin_not_editable():
    from app.data_providers import custom as cs

    assert cs.get_config_dict("stocksdk") is None
    for fn in (lambda: cs.save_config("stocksdk", {}), lambda: cs.delete_config("stocksdk")):
        try:
            fn()
            raise AssertionError("expected ValueError for builtin")
        except ValueError:
            pass
