"""F10 回测转监控规则端点测试 — 映射契约/幂等/错误码/引擎同步。

全部落 tmp_path, 不读行情不写真实 data/。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.backtest.run_store import BacktestRun, BacktestRunStore, RunSubject
from app.strategy import monitor_rules


class _StubEngine:
    """记录 set_rules 调用的引擎替身。"""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def set_rules(self, rules):
        self.calls.append(list(rules))


def _build_app(tmp_path: Path, engine=None) -> FastAPI:
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    if engine is not None:
        app.state.monitor_engine = engine
    return app


def _make_run(run_id: str = "btrun00001", kind: str = "strategy", **cfg_overrides) -> BacktestRun:
    cfg = {
        "strategy_id": "macd",
        "symbols": ["600000.SH", "000001.SZ"],
        "start": "2026-01-01",
        "end": "2026-06-30",
    }
    cfg.update(cfg_overrides)
    return BacktestRun(
        run_id=run_id,
        kind=kind,
        created_at="2026-08-19T00:00:00+00:00",
        subject=RunSubject(id="macd", name="MACD金叉", hash="h1"),
        config=cfg,
        stats={"sharpe": 1.5},
        equity_curve=[{"date": "2026-01-02", "equity": 1.01}],
    )


# ── 成功创建 + 字段映射 ──────────────────────────────────


def test_create_maps_all_fields(tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run())
    client = TestClient(_build_app(tmp_path))

    resp = client.post("/api/backtest/runs/btrun00001/to-monitor-rule")

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    rule = body["rule"]
    assert rule["id"] == "mr_bt_btrun000"
    assert rule["name"] == "回测:MACD金叉"
    assert rule["enabled"] is True
    assert rule["type"] == "strategy"
    assert rule["scope"] == "symbols"
    assert rule["symbols"] == ["600000.SH", "000001.SZ"]
    assert rule["strategy_id"] == "macd"
    assert rule["direction"] == "entry"
    assert rule["cooldown_seconds"] == 3600
    assert rule["severity"] == "info"
    assert rule["message"] == "由回测运行 btrun000 创建"


def test_symbols_pool_maps_to_scope_symbols(tmp_path: Path):
    """symbols 池非空 → scope=symbols + 原样列表。"""
    BacktestRunStore(tmp_path).save(
        _make_run("btrun00002", symbols=["510300.SH"])
    )
    client = TestClient(_build_app(tmp_path))

    body = client.post("/api/backtest/runs/btrun00002/to-monitor-rule").json()

    assert body["rule"]["scope"] == "symbols"
    assert body["rule"]["symbols"] == ["510300.SH"]


def test_empty_symbols_maps_to_scope_all(tmp_path: Path):
    """symbols 为空 → scope=all, 不携带股票列表。"""
    BacktestRunStore(tmp_path).save(_make_run("btrun00003", symbols=[]))
    client = TestClient(_build_app(tmp_path))

    body = client.post("/api/backtest/runs/btrun00003/to-monitor-rule").json()

    assert body["rule"]["scope"] == "all"
    assert body["rule"]["symbols"] == []


# ── 幂等 ────────────────────────────────────────────────


def test_idempotent_second_call_returns_existing(tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run())
    client = TestClient(_build_app(tmp_path))

    first = client.post("/api/backtest/runs/btrun00001/to-monitor-rule").json()
    first_created_at = first["rule"]["created_at"]
    second = client.post("/api/backtest/runs/btrun00001/to-monitor-rule").json()

    assert second["created"] is False
    assert second["rule"]["id"] == first["rule"]["id"]
    # 返回的是已落盘规则, 不是重新生成 (created_at 不变)
    assert second["rule"]["created_at"] == first_created_at
    # 目录里只有一份规则文件
    saved = monitor_rules.load_all(tmp_path)
    assert [r["id"] for r in saved] == ["mr_bt_btrun000"]


# ── 错误码 ──────────────────────────────────────────────


def test_missing_run_returns_404(tmp_path: Path):
    client = TestClient(_build_app(tmp_path))

    resp = client.post("/api/backtest/runs/missing123/to-monitor-rule")

    assert resp.status_code == 404


def test_factor_run_returns_400(tmp_path: Path):
    BacktestRunStore(tmp_path).save(
        _make_run("facrun0001", kind="factor", symbols=[], strategy_id="mom12")
    )
    client = TestClient(_build_app(tmp_path))

    resp = client.post("/api/backtest/runs/facrun0001/to-monitor-rule")

    assert resp.status_code == 400
    assert "策略" in resp.json()["detail"]


def test_strategy_run_without_strategy_id_returns_400(tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run("noidrun01", strategy_id=""))
    client = TestClient(_build_app(tmp_path))

    resp = client.post("/api/backtest/runs/noidrun01/to-monitor-rule")

    assert resp.status_code == 400


# ── 引擎内存态同步 ─────────────────────────────────────


def test_engine_rules_synced_after_create(tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run())
    engine = _StubEngine()
    client = TestClient(_build_app(tmp_path, engine=engine))

    client.post("/api/backtest/runs/btrun00001/to-monitor-rule")

    assert len(engine.calls) == 1
    assert [r["id"] for r in engine.calls[0]] == ["mr_bt_btrun000"]
