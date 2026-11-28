"""安全、稳定性与契约修复回归测试 (Batch 2):
- macOS osascript 通知改 argv 传参, 彻底杜绝 AppleScript 注入
- analysis_menus / JsonDefinitionStore 路径穿越防护 (POSIX & Windows %5C)
- preferences.load() 与 save() 并发无 TOCTOU / None 返回
- GET /api/openapi.json?tier=a 不原地篡改 app.openapi() 缓存
- DepthService.boot_check() 周末/盘前重启不覆写前交易日定版封单
- MCP server get_kline 传 days 而非 limit, 且路径参数正确 URL 编码
"""
from __future__ import annotations

import importlib.util
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.analysis import router as analysis_router
from app.definitions import JsonDefinitionStore
from app.market_time import CN_TZ
from app.services import depth_service as depth_mod
from app.services import notify_adapter, preferences


# ── 1. macOS AppleScript 参数注入防护 ────────────────────────────────
def test_notify_osascript_passes_argv_without_script_interpolation(monkeypatch):
    captured: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(notify_adapter.subprocess, "run", _fake_run)
    payload_title = 'rule\\" & do shell script "id" & "'
    payload_msg = 'line1\n" & do shell script "whoami'
    assert notify_adapter._notify_osascript(payload_title, payload_msg) is True
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "osascript"
    assert "--" in cmd
    sep = cmd.index("--")
    # 脚本片段本身不含任何用户输入
    script_parts = " ".join(cmd[:sep])
    assert "do shell script" not in script_parts
    assert cmd[sep + 1 :] == [payload_title, payload_msg]


# ── 2. 路径穿越防护 (analysis_menus & JsonDefinitionStore) ────────────
def test_json_definition_store_rejects_path_traversal(tmp_path: Path):
    store = JsonDefinitionStore("user_data/custom_factors", label="custom factor")
    for bad_id in ("", ".", "..", "../auth", r"..\auth", "a/b", r"a\b", "foo..bar", "ok\x00bad", "C:evil", "foo:stream"):
        with pytest.raises(ValueError):
            store.path(tmp_path, bad_id)
    # 合法标识放行
    p = store.path(tmp_path, "my_factor-1")
    assert p.parent == (tmp_path / "user_data" / "custom_factors").resolve()


def test_analysis_delete_menu_rejects_traversal(tmp_path: Path):
    secret = tmp_path / "user_data" / "auth.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"hash":"keep"}', encoding="utf-8")

    app = FastAPI()
    app.include_router(analysis_router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client = TestClient(app)

    # Windows %5C 编码反斜杠穿越测试
    res = client.delete("/api/analysis-menus/..%5Cuser_data%5Cauth")
    assert res.status_code == 400
    assert secret.exists()


# ── 3. preferences.load() 与 save() 并发安全 ──────────────────────────
def test_preferences_concurrent_load_and_save_never_returns_none(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    preferences.save({"k": 0})

    errors: list[BaseException] = []
    stop = threading.Event()

    def _writer():
        try:
            for i in range(1, 60):
                preferences.save({"k": i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stop.set()

    def _reader():
        try:
            while not stop.is_set():
                data = preferences.load()
                assert isinstance(data, dict)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_writer), *(threading.Thread(target=_reader) for _ in range(4))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []


# ── 4. /api/openapi.json?tier=a 不污染 app.openapi() 缓存 ────────────
def test_openapi_tier_a_does_not_mutate_cached_full_schema():
    from app.main import app

    full_before = len(app.openapi().get("paths", {}))
    client = TestClient(app)
    r_a = client.get("/api/openapi.json?tier=a")
    assert r_a.status_code == 200
    tier_a_count = len(r_a.json().get("paths", {}))
    assert 0 < tier_a_count < full_before

    # 原缓存及 ?tier=all 必须保持完整
    assert len(app.openapi().get("paths", {})) == full_before
    r_all = client.get("/api/openapi.json?tier=all")
    assert r_all.status_code == 200
    assert len(r_all.json().get("paths", {})) == full_before


# ── 5. DepthService.boot_check() 周末/盘前不覆写历史定版封单 ──────────
def test_depth_boot_check_does_not_overwrite_previous_day_on_weekend_or_premarket(tmp_path: Path, monkeypatch):
    friday = date(2026, 11, 27)
    saturday = date(2026, 11, 28)

    # 预写周五定版 depth5 parquet
    fri_part = tmp_path / "depth5" / f"date={friday.isoformat()}" / "part.parquet"
    fri_part.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["000001.SZ"],
        "sealed_up": [True],
        "sealed_down": [False],
        "ask1_vol": [0],
        "bid1_vol": [50000],
        "status": ["sealed_up"],
        "fetched_at": [1700000000.0],
    }).write_parquet(fri_part)

    svc = depth_mod.DepthService()
    enriched_df = pl.DataFrame({"symbol": ["000001.SZ"], "signal_limit_up": [True]})
    svc.set_repo(SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest=lambda: (enriched_df, friday),
    ))
    monkeypatch.setattr(svc, "_has_capability", lambda: True)
    monkeypatch.setattr(depth_mod, "cn_today", lambda: saturday)
    monkeypatch.setattr(depth_mod, "cn_now", lambda: datetime(2026, 11, 28, 10, 0, tzinfo=CN_TZ))

    finalized = {"called": False}
    monkeypatch.setattr(svc, "finalize", lambda: finalized.__setitem__("called", True))

    svc.boot_check()
    assert finalized["called"] is False
    assert svc._sealed_date == friday
    assert "000001.SZ" in svc._sealed_cache

    # 即使周五 parquet 不存在, 周六启动也不能拿非交易时段盘口跑 finalize
    fri_part.unlink()
    svc2 = depth_mod.DepthService()
    svc2.set_repo(SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest=lambda: (enriched_df, friday),
    ))
    monkeypatch.setattr(svc2, "_has_capability", lambda: True)
    monkeypatch.setattr(svc2, "finalize", lambda: finalized.__setitem__("called", True))
    svc2.boot_check()
    assert finalized["called"] is False


# ── 6. MCP Server get_kline 参数与路径转义 ────────────────────────────
def test_mcp_server_get_kline_uses_days_and_quotes_path_params(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    server_path = repo_root / "mcp-server" / "src" / "tsp_mcp_server" / "server.py"
    if not server_path.exists():
        pytest.skip("mcp-server not present")
    if importlib.util.find_spec("mcp") is None:
        # 无 mcp 依赖时直接做 AST 静态契约校验, 确保 get_kline 传 days 且使用 quote
        source = server_path.read_text(encoding="utf-8")
        assert '"days": days' in source
        assert '"limit": limit' not in source.split("def get_kline")[1].split("def get_index_kline")[0]
        assert "quote(strategy_id, safe='')" in source
        assert "quote(table, safe='')" in source
        return

    monkeypatch.setenv("TSP_SCOPES", "read:market,read:analysis,read:ext,write:ext,run:backtest")
    spec = importlib.util.spec_from_file_location("tsp_mcp_server_test_mod", server_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls: list[tuple] = []
    monkeypatch.setattr(mod, "_call", lambda method, path, **kw: (calls.append((method, path, kw)) or "ok"))
    mod.get_kline("000001.SZ", days=45)
    mod.get_strategy_detail("../secret")
    mod.query_ext_table("a/b", limit=5)

    assert calls[0] == ("GET", "/api/kline/daily", {"params": {"symbol": "000001.SZ", "days": 45}})
    assert calls[1][1] == "/api/strategies/..%2Fsecret"
    assert calls[2][1] == "/api/ext-data/a%2Fb/rows"
