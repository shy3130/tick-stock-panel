"""BacktestRun API 端点隔离测试 — 关键契约: 路由/持久化/PATCH 422/穿越/比较/导出/复跑。

stub 掉真实回测引擎, 全部落 tmp_path, 不读行情不写真实 data/。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.backtest.run_store import BacktestRun, BacktestRunStore, RunSubject
from app.services.research_registry import ResearchStore


@dataclass
class _StubResult:
    run_id: str = "stubrun001"
    config: dict = field(default_factory=lambda: {
        "strategy_id": "macd", "symbols": ["600000.SH"],
        "start": "2026-01-01", "end": "2026-06-30",
        "fees_pct": 0.0002, "slippage_bps": 5.0, "matching": "open_t+1",
    })
    stats: dict = field(default_factory=lambda: {"sharpe": 1.5, "total_return": 0.2})
    equity_curve: list = field(default_factory=lambda: [{"date": "2026-01-02", "equity": 1.01}])
    drawdown_curve: list = field(default_factory=lambda: [])
    benchmark_curve: list = field(default_factory=lambda: [])
    trades: list = field(default_factory=list)
    per_symbol_stats: list = field(default_factory=list)
    strategy_info: dict = field(default_factory=lambda: {"id": "macd", "name": "MACD", "source": "builtin"})
    elapsed_ms: float = 1.0
    error: str | None = None


class _StubStrategyService:
    def __init__(self, *a, **kw):
        pass

    def run(self, cfg, progress_cb=None, cancel_event=None, **kw):
        return _StubResult()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = SimpleNamespace()
    return TestClient(app)


def _make_run(run_id: str = "apirun0001", **overrides) -> BacktestRun:
    defaults = dict(
        run_id=run_id,
        kind="strategy",
        created_at="2026-08-19T00:00:00+00:00",
        subject=RunSubject(id="macd", name="MACD", hash="h1"),
        config={
            "strategy_id": "macd", "symbols": ["600000.SH"],
            "start": "2026-01-01", "end": "2026-06-30",
            "fees_pct": 0.0002, "slippage_bps": 5.0,
        },
        data_snapshot={
            "snapshot_hash": "snap-1", "canonical_generation": "g1",
            "universe_definition": {"hash": "u1"},
        },
        benchmark={"symbol": "000001.INDEX"},
        metric_context={"version": "1"},
        engine_version="polars-numpy-v1",
        stats={"sharpe": 1.5, "total_return": 0.2},
        equity_curve=[{"date": "2026-01-02", "equity": 1.01}],
        trades=[{"symbol": "600000.SH", "pnl": 100.0}],
    )
    defaults.update(overrides)
    return BacktestRun(**defaults)


# ── 读取/列表 ────────────────────────────────────────────


def test_saved_run_survives_restart(client: TestClient, tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run())
    body = client.get("/api/backtest/runs/apirun0001").json()
    assert body["equity_curve"][0]["equity"] == 1.01
    assert body["trades"][0]["symbol"] == "600000.SH"


def test_list_returns_lightweight_summary(client: TestClient, tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run())
    item = client.get("/api/backtest/runs").json()["items"][0]
    assert "equity_curve" not in item and "trades" not in item
    assert item["stats"]["sharpe"] == 1.5



def test_save_backtest_run_is_idempotent_when_only_created_at_differs(tmp_path: Path):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(
            store=SimpleNamespace(data_dir=tmp_path),
        ))),
    )
    original = _make_run("samefact01", created_at="2026-08-19T00:00:00+00:00")
    replay = _make_run("samefact01", created_at="2026-08-19T00:01:00+00:00")

    assert backtest_api._save_backtest_run(request, original) is not None
    saved = backtest_api._save_backtest_run(request, replay)

    assert saved is not None
    assert saved.created_at == original.created_at

def test_static_compare_route_not_captured(client: TestClient):
    """POST /runs/compare 是独立静态路由, 不被 {run_id} 截获。"""
    resp = client.post("/api/backtest/runs/compare", json={"run_ids": ["aaa", "bbb"]})
    assert resp.status_code == 404  # run 不存在, 而非路由 405


def test_missing_run_404_and_traversal_blocked(client: TestClient, tmp_path: Path):
    assert client.get("/api/backtest/runs/missing123").status_code == 404
    for bad in ["..", "../etc/passwd"]:
        assert client.get(f"/api/backtest/runs/{bad}").status_code in (400, 404)
    assert not (tmp_path.parent / "etc").exists()


def test_corrupt_run_file_returns_404_not_500(client: TestClient, tmp_path: Path):
    run_dir = tmp_path / "research" / "backtest_runs"
    run_dir.mkdir(parents=True)
    (run_dir / "brokenrun1.json").write_text("{}", encoding="utf-8")

    assert client.get("/api/backtest/runs/brokenrun1").status_code == 404


# ── PATCH ────────────────────────────────────────────────


def test_patch_only_favorite_label_other_fields_422(client: TestClient, tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run())
    resp = client.patch("/api/backtest/runs/apirun0001", json={"favorite": True, "label": "对照组"})
    assert resp.status_code == 200
    assert BacktestRunStore(tmp_path).get("apirun0001").label == "对照组"

    for field_name, value in [("stats", {"x": 1}), ("run_id", "hacked0000")]:
        assert client.patch("/api/backtest/runs/apirun0001", json={field_name: value}).status_code == 422
    assert BacktestRunStore(tmp_path).get("apirun0001").stats["sharpe"] == 1.5


# ── 比较/导出 ────────────────────────────────────────────


def test_compare_two_runs_with_warnings(client: TestClient, tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run("cmpaaaaa1"))
    store.save(_make_run(
        "cmpbbbbb2",
        stats={"sharpe": 0.8},
        config={"strategy_id": "macd", "start": "2026-02-01", "end": "2026-07-31"},
        data_snapshot={"snapshot_hash": "snap-2", "canonical_generation": "g2", "universe_definition": {"hash": "u2"}},
        metric_context={"version": "2"},
    ))
    body = client.post("/api/backtest/runs/compare", json={"run_ids": ["cmpaaaaa1", "cmpbbbbb2"]}).json()
    assert body["metric_matrix"]["sharpe"]["cmpaaaaa1"] == 1.5
    assert len(body["curves"]) == 2
    assert any("compare." in w for w in body["warnings"])


def test_compare_count_bounds_422(client: TestClient):
    assert client.post("/api/backtest/runs/compare", json={"run_ids": ["a"]}).status_code == 422
    assert client.post("/api/backtest/runs/compare", json={"run_ids": [f"r{i}" for i in range(5)]}).status_code == 422


def test_compare_returns_config_diff_and_trade_summary(client: TestClient, tmp_path: Path):
    """config_diff / trade_summary 相对首个 run (baseline) 端到端透传, 不改既有 schema。"""
    store = BacktestRunStore(tmp_path)
    store.save(_make_run(
        "cmpaaaaa1",
        config={
            **_make_run().config,
            "params": {"fast": 12, "slow": 26},
        },
        trades=[
            {"symbol": "600000.SH", "entry_date": "2026-01-05", "exit_date": "2026-01-10", "shares": 100, "entry_value": 1000.0, "pnl_pct": 0.05},
            {"symbol": "600519.SH", "entry_date": "2026-02-01", "exit_date": "2026-02-15", "shares": 10, "entry_value": 18000.0, "pnl_pct": -0.02},
        ],
    ))
    store.save(_make_run(
        "cmpbbbbb2",
        config={
            **_make_run().config,
            "params": {"fast": 12, "slow": 30},
        },
        trades=[
            {"symbol": "600000.SH", "entry_date": "2026-01-05", "exit_date": "2026-01-10", "shares": 200, "entry_value": 2000.0, "pnl_pct": 0.06},
        ],
    ))
    body = client.post("/api/backtest/runs/compare", json={"run_ids": ["cmpaaaaa1", "cmpbbbbb2"]}).json()

    # 既有 schema 不破
    assert set(body) >= {"runs", "metric_matrix", "curves", "warnings"}
    diff = body["config_diff"]
    assert diff["baseline_run_id"] == "cmpaaaaa1"
    cand = diff["candidates"][0]
    assert cand["run_id"] == "cmpbbbbb2"
    assert {(e["path"], e["op"]) for e in cand["entries"]} == {("params.slow", "changed")}
    assert cand["entries"][0]["before"] == 26 and cand["entries"][0]["after"] == 30

    summary = body["trade_summary"]
    assert summary["baseline_run_id"] == "cmpaaaaa1"
    assert summary["baseline_n_trades"] == 2
    tc = summary["candidates"][0]
    assert (tc["common"], tc["common_value_diff"], tc["added"], tc["removed"]) == (1, 1, 0, 1)
    row = tc["samples"]["common"][0]
    assert row["value_differs"] is True
    assert row["baseline"]["shares"] == 100 and row["candidate"]["shares"] == 200
    assert tc["samples"]["removed"][0]["symbol"] == "600519.SH"


def test_export_json_and_csv(client: TestClient, tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run())
    j = client.get("/api/backtest/runs/apirun0001/export", params={"fmt": "json"})
    assert j.status_code == 200 and "equity_curve" in j.text
    c = client.get("/api/backtest/runs/apirun0001/export", params={"fmt": "csv"})
    assert c.status_code == 200 and "600000.SH" in c.text
    assert client.get("/api/backtest/runs/apirun0001/export", params={"fmt": "xlsx"}).status_code == 400


def test_export_csv_factor_group_stats(client: TestClient, tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run(
        "apifactor1", kind="factor", trades=[],
        factor_result={"group_stats": [{"group": 1, "ann_return": 0.1}]},
    ))
    assert "ann_return" in client.get("/api/backtest/runs/apifactor1/export", params={"fmt": "csv"}).text


# ── 删除 ─────────────────────────────────────────────────


def test_delete_run_and_legacy_refused(client: TestClient, tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run())
    assert client.delete("/api/backtest/runs/apirun0001").status_code == 200
    assert client.delete("/api/backtest/runs/apirun0001").status_code == 404

    ResearchStore(tmp_path).save_run_card(
        run_id="legacyxyz1", kind="strategy", config={"strategy_id": "macd"}, stats={"sharpe": 1.0},
    )
    assert client.delete("/api/backtest/runs/legacyxyz1").status_code == 403
    assert (tmp_path / "research" / "run_cards" / "legacyxyz1.json").exists()


# ── 复跑 ─────────────────────────────────────────────────


def test_rerun_creates_new_run_with_source(client: TestClient, tmp_path: Path, monkeypatch):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run("origrun001"))
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _StubStrategyService)
    resp = client.post("/api/backtest/runs/origrun001/rerun")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] != "origrun001"
    assert body["source_run_id"] == "origrun001"
    assert store.get("origrun001").source_run_id is None  # 原 Run 不改
    assert store.exists(body["run_id"])  # 新 Run 已持久化


def test_rerun_legacy_card_without_strategy_id_400(client: TestClient, tmp_path: Path):
    ResearchStore(tmp_path).save_run_card(
        run_id="legacynoid", kind="strategy", config={"start": "2026-01-01"}, stats={},
    )
    assert client.post("/api/backtest/runs/legacynoid/rerun").status_code == 400


# ── SSE 取消 job_key / 非法参数 ──────────────────────────


def _register_stream_job(**overrides) -> tuple[str, object]:
    """按与 strategy_stream/cancel 相同的口径注册一个运行中任务。"""
    args = dict(
        strategy_id="macd", symbols=None, start="2026-01-01", end="2026-06-30",
        matching="open_t+1", entry_fill=None, exit_fill=None,
        fees_pct=0.0002, stamp_tax_pct=0.0005, slippage_bps=5.0, max_positions=10, max_exposure_pct=1.0,
        initial_capital=1_000_000.0, position_sizing="equal",
        params=None, overrides=None, mode="position", holding_days=5,
        regime_filter=None, benchmark_symbol="000001.INDEX", risk_free_rate=0.03,
    )
    args.update(overrides)
    key = backtest_api._make_job_key(**args)
    job = backtest_api._BacktestJob(key)
    backtest_api._running_jobs[key] = job
    return key, job


def test_strategy_cancel_matches_running_job_with_nonzero_risk_free_rate(client: TestClient):
    """取消接口必须把 risk_free_rate 计入 job_key, 否则 rf≠0 的任务永远无法取消。"""
    key, job = _register_stream_job()
    try:
        ok = client.post("/api/backtest/strategy/cancel", json={
            "qs": "strategy_id=macd&start=2026-01-01&end=2026-06-30&risk_free_rate=0.03",
        })
        assert ok.status_code == 200 and ok.json() == {"ok": True}
        assert job.cancel_event.is_set()

        # rf 不一致 (缺省 0) → job_key 不同 → 找不到任务
        miss = client.post("/api/backtest/strategy/cancel", json={
            "qs": "strategy_id=macd&start=2026-01-01&end=2026-06-30",
        })
        assert miss.json() == {"ok": False, "message": "任务不存在或已完成"}
    finally:
        backtest_api._running_jobs.pop(key, None)


def test_strategy_stream_rejects_illegal_benchmark_and_risk_free(client: TestClient):
    # F9 后 benchmark_symbol 接受任意 6 位码/合法后缀; 非法格式仍开流前 422
    assert client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "macd", "benchmark_symbol": "not-a-code",
    }).status_code == 422
    # benchmark_symbol 与 benchmark_run_id 同给 (非默认 symbol) → 互斥 422
    assert client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "macd",
        "benchmark_symbol": "000300.INDEX",
        "benchmark_run_id": "run00001",
    }).status_code == 422
    assert client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "macd", "risk_free_rate": -1.5,
    }).status_code == 422
    assert client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "macd", "risk_free_rate": 2.0,
    }).status_code == 422


def test_strategy_stream_rejects_illegal_dates_before_stream_starts(client: TestClient):
    """非法 ISO 日期必须在开流前结构化 422, 而不是 500。"""
    assert client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "macd", "end": "not-a-date",
    }).status_code == 422
    assert client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "macd", "start": "2026-02-30", "end": "2026-06-30",
    }).status_code == 422


def test_strategy_stream_rejects_illegal_json_params_before_stream_starts(client: TestClient):
    """params/overrides/regime_filter 非法 JSON 或非对象在开流前 422, 不在开流后断流。"""
    base = {"strategy_id": "macd", "start": "2026-01-01", "end": "2026-06-30"}
    for key in ("params", "overrides", "regime_filter"):
        assert client.get("/api/backtest/strategy/stream", params={**base, key: "{not-json"}).status_code == 422
        assert client.get("/api/backtest/strategy/stream", params={**base, key: "[1,2]"}).status_code == 422


def test_strategy_stream_valid_params_still_stream(client: TestClient, monkeypatch):
    """校验前移不影响合法 SSE: 正常开流并推送 done 事件, job_key 口径不变。"""
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _StubStrategyService)
    client.app.state.backtest_engine = object()  # 跳过真实引擎构建
    key = backtest_api._make_job_key(
        "macd", None, "2026-01-01", "2026-06-30",
        "open_t+1", None, None, 0.0002, 0.0005, 5.0, 10, 1.0, 1_000_000.0, "equal",
        '{"lookback": 20}', None, "position", 5, None, "000001.INDEX", 0.0,
    )
    try:
        with client.stream(
            "GET", "/api/backtest/strategy/stream",
            params={
                "strategy_id": "macd", "start": "2026-01-01", "end": "2026-06-30",
                "params": '{"lookback": 20}',
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            text = resp.read().decode()
        assert "event: done" in text
    finally:
        backtest_api._running_jobs.pop(key, None)


def test_robustness_rejects_out_of_range_resampling_counts(client: TestClient):
    """n_boot/n_perm 越界在任何昂贵重计算前被请求校验 422, 不触引擎。"""
    for body in (
        {"strategy_id": "macd", "n_boot": 0},
        {"strategy_id": "macd", "n_boot": 100001},
        {"strategy_id": "macd", "n_perm": 0},
        {"strategy_id": "macd", "n_perm": 50000},
    ):
        assert client.post("/api/backtest/strategy/robustness", json=body).status_code == 422


# ── 复跑口径 / 重放幂等回归 ────────────────────────────────


def test_rerun_factor_keeps_rebalance_metric_context(client: TestClient, tmp_path: Path, monkeypatch):
    """因子复跑的 run 级 metric_context 必须保持 rebalance 频率与 rf, 不得回退 daily/0。"""
    from app.backtest.factor import FactorResult
    from app.backtest.metrics import MetricContext

    store = BacktestRunStore(tmp_path)
    store.save(_make_run(
        "origfact01",
        kind="factor",
        config={
            "factor_name": "test_factor", "symbols": None,
            "start": "2026-01-01", "end": "2026-06-30",
            "n_groups": 5, "rebalance": "monthly", "weight": "equal",
            "risk_free_rate": 0.03,
        },
    ))

    monthly = MetricContext("monthly", risk_free_rate=0.03)

    class _StubFactorService:
        def __init__(self, *a, **kw):
            pass

        def run(self, cfg):
            assert cfg.rebalance == "monthly"
            assert cfg.risk_free_rate == pytest.approx(0.03)
            return FactorResult(
                run_id="factrerun01",
                config={
                    "factor_name": cfg.factor_name, "symbols": cfg.symbols,
                    "start": str(cfg.start), "end": str(cfg.end),
                    "n_groups": cfg.n_groups, "rebalance": cfg.rebalance,
                    "weight": cfg.weight, "fees_pct": cfg.fees_pct,
                    "slippage_bps": cfg.slippage_bps,
                    "risk_free_rate": cfg.risk_free_rate,
                },
                metric_context=monthly.to_dict(),
            )

    monkeypatch.setattr("app.backtest.factor.FactorBacktestService", _StubFactorService)
    resp = client.post("/api/backtest/runs/origfact01/rerun")
    assert resp.status_code == 200, resp.text
    context = resp.json()["metric_context"]
    assert context["return_frequency"] == "monthly"
    assert context["periods_per_year"] == 12
    assert context["risk_free_rate"] == pytest.approx(0.03)
    persisted = store.get(resp.json()["run_id"])
    assert persisted.metric_context["return_frequency"] == "monthly"
    assert persisted.source_run_id == "origfact01"


def test_save_backtest_run_replay_after_patch_keeps_patched_metadata(tmp_path: Path):
    """用户 PATCH 收藏/标签后, SSE 重放同一事实仍是幂等的, 不误报冲突。"""
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(
            store=SimpleNamespace(data_dir=tmp_path),
        ))),
    )
    assert backtest_api._save_backtest_run(request, _make_run("patched001")) is not None
    BacktestRunStore(tmp_path).patch("patched001", favorite=True, label="对照组")

    replay = _make_run("patched001", created_at="2026-08-19T01:00:00+00:00")
    saved = backtest_api._save_backtest_run(request, replay)

    assert saved is not None
    assert saved.favorite is True
    assert saved.label == "对照组"
