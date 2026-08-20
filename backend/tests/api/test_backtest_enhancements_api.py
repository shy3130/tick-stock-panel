"""回测增强 API 契约隔离测试 — A1/B6 字段透传 + 四个诊断端点 + A3 交易净值带。

stub 掉真实引擎与 provider, 数据全落 tmp_path, 不读行情不写真实 data/。
覆盖: 三新字段透传 (strategy_run/robustness)、上市日期 fail-soft、
regime-breakdown / cost-sensitivity / style-attribution / fill-reachability
的请求校验与响应形状。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.backtest.run_store import BacktestRun, BacktestRunStore


# ── 桩: 回测服务 / 引擎 / provider ────────────────────────


@dataclass
class _StubResult:
    run_id: str
    config: dict = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: {
        "sharpe": 1.0, "total_return": 0.1, "n_trades": 0,
    })
    equity_curve: list = field(default_factory=list)
    drawdown_curve: list = field(default_factory=list)
    benchmark_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    per_symbol_stats: list = field(default_factory=list)
    strategy_info: dict = field(default_factory=lambda: {"id": "macd", "source": "builtin"})
    elapsed_ms: float = 1.0
    error: str | None = None


class _StubEngine:
    """load_panel 可配置的引擎桩 (style-attribution 用)。"""

    def __init__(self):
        self.panel = pl.DataFrame()
        self.load_calls: list[tuple] = []

    def load_panel(self, symbols, start, end, columns=None):
        self.load_calls.append((symbols, start, end))
        return self.panel


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        enriched_latest_date=lambda: None,
    )
    app.state.strategy_engine = SimpleNamespace()
    app.state.backtest_engine = _StubEngine()
    return TestClient(app)


def _base_body(**overrides) -> dict:
    body = {
        "strategy_id": "macd",
        "symbols": ["600000.SH"],
        "start": "2026-01-01",
        "end": "2026-06-30",
    }
    body.update(overrides)
    return body


class _CapturingService:
    """记录每次 run 的 cfg 与 kwargs; result_trades 可配置结果成交列表。"""

    calls: list[dict] = []
    result_trades: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def run(self, cfg, progress_cb=None, cancel_event=None, **kwargs):
        self.calls.append({"cfg": cfg, "kwargs": kwargs})
        return _StubResult(
            run_id=f"caprun{len(self.calls):03d}",
            trades=list(self.result_trades),
        )


@pytest.fixture()
def capturing_service(monkeypatch):
    """挂桩服务 + 隔离 provenance 外部历史; 每个测试独立 calls 列表。"""
    monkeypatch.setattr(
        "app.services.canonical_history.resolve_published_history", lambda _root=None: None
    )
    monkeypatch.setattr(
        "app.backtest.strategy.StrategyBacktestService", _CapturingService
    )
    _CapturingService.calls = []
    _CapturingService.result_trades = []
    return _CapturingService


# ── A1/B6 字段透传 ────────────────────────────────────────


def test_strategy_run_passes_new_fields_and_listing_dates(
    client: TestClient, capturing_service, monkeypatch
):
    listing_df = pl.DataFrame({
        "symbol": ["600000.SH", "600519.SH"],
        "listing_date": [date(2020, 1, 1), date(2010, 1, 1)],
    })
    monkeypatch.setattr(backtest_api, "_strategy_listing_dates", lambda: listing_df)

    resp = client.post("/api/backtest/strategy/run", json=_base_body(
        max_participation_pct=0.05,
        participation_volume_window=10,
        min_listed_days=90,
    ))
    assert resp.status_code == 200, resp.text
    assert len(capturing_service.calls) == 1
    cfg = capturing_service.calls[0]["cfg"]
    assert cfg.max_participation_pct == 0.05
    assert cfg.participation_volume_window == 10
    assert cfg.min_listed_days == 90
    # listing_dates 原样传给 service.run, 不在 API 层做门控
    assert capturing_service.calls[0]["kwargs"].get("listing_dates") is listing_df


def test_strategy_run_defaults_and_no_listing_fetch_when_gate_disabled(
    client: TestClient, capturing_service, monkeypatch
):
    def _boom():
        raise AssertionError("min_listed_days=0 时不应取上市日期表")

    monkeypatch.setattr(backtest_api, "_strategy_listing_dates", _boom)
    resp = client.post("/api/backtest/strategy/run", json=_base_body())
    assert resp.status_code == 200, resp.text
    cfg = capturing_service.calls[0]["cfg"]
    assert cfg.max_participation_pct is None
    assert cfg.participation_volume_window == 5
    assert cfg.min_listed_days == 0
    assert capturing_service.calls[0]["kwargs"].get("listing_dates") is None


def test_strategy_run_listing_dates_fail_soft_to_none(
    client: TestClient, capturing_service, monkeypatch
):
    """provider 不可用 → listing_dates=None, 请求仍成功 (service 侧告警跳过门控)。"""
    monkeypatch.setattr(backtest_api, "_strategy_listing_dates", lambda: None)
    resp = client.post("/api/backtest/strategy/run", json=_base_body(min_listed_days=90))
    assert resp.status_code == 200, resp.text
    assert capturing_service.calls[0]["kwargs"].get("listing_dates") is None


def test_new_fields_rejected_out_of_range(client: TestClient):
    assert client.post("/api/backtest/strategy/run", json=_base_body(
        max_participation_pct=1.5
    )).status_code == 422
    assert client.post("/api/backtest/strategy/run", json=_base_body(
        max_participation_pct=0.0
    )).status_code == 422
    assert client.post("/api/backtest/strategy/run", json=_base_body(
        participation_volume_window=0
    )).status_code == 422
    assert client.post("/api/backtest/strategy/run", json=_base_body(
        min_listed_days=-1
    )).status_code == 422
    assert client.post("/api/backtest/strategy/run", json=_base_body(
        min_listed_days=4000
    )).status_code == 422


def test_robustness_passes_new_fields_and_listing_dates_to_every_run(
    client: TestClient, capturing_service, monkeypatch
):
    listing_df = pl.DataFrame({
        "symbol": ["600000.SH"], "listing_date": [date(2020, 1, 1)],
    })
    monkeypatch.setattr(backtest_api, "_strategy_listing_dates", lambda: listing_df)

    resp = client.post("/api/backtest/strategy/robustness", json=_base_body(
        n_folds=2,
        parameter_perturbation=False,
        walk_forward_enabled=False,
        bootstrap=False,
        mc_permutation=False,
        max_participation_pct=0.2,
        participation_volume_window=20,
        min_listed_days=30,
    ))
    assert resp.status_code == 200, resp.text
    assert len(capturing_service.calls) == 1 + 2  # full + 2 折
    for call in capturing_service.calls:
        cfg = call["cfg"]
        assert cfg.max_participation_pct == 0.2
        assert cfg.participation_volume_window == 20
        assert cfg.min_listed_days == 30
        assert call["kwargs"].get("listing_dates") is listing_df


# ── _strategy_listing_dates 单元契约 ──────────────────────


def test_strategy_listing_dates_selects_symbol_listing_columns(monkeypatch):
    class _P:
        def get_stock_reference_flags(self):
            return pl.DataFrame({
                "symbol": ["600000.SH"],
                "listing_date": [date(2020, 1, 1)],
                "is_ah": [False],
            })

    monkeypatch.setattr(backtest_api, "_get_data_provider", lambda capability: _P())
    df = backtest_api._strategy_listing_dates()
    assert df is not None
    assert df.columns == ["symbol", "listing_date"]


def test_strategy_listing_dates_fail_closed_variants(monkeypatch):
    # provider 缺特有方法 (不在 base 契约)
    monkeypatch.setattr(backtest_api, "_get_data_provider", lambda capability: object())
    assert backtest_api._strategy_listing_dates() is None

    # 空 df
    class _Empty:
        def get_stock_reference_flags(self):
            return pl.DataFrame()

    monkeypatch.setattr(backtest_api, "_get_data_provider", lambda capability: _Empty())
    assert backtest_api._strategy_listing_dates() is None

    # 查询抛异常
    class _Boom:
        def get_stock_reference_flags(self):
            raise RuntimeError("fstore down")

    monkeypatch.setattr(backtest_api, "_get_data_provider", lambda capability: _Boom())
    assert backtest_api._strategy_listing_dates() is None

    # provider 解析失败
    def _no_provider(capability):
        raise RuntimeError("no provider")

    monkeypatch.setattr(backtest_api, "_get_data_provider", _no_provider)
    assert backtest_api._strategy_listing_dates() is None

    # 缺列
    class _MissingCols:
        def get_stock_reference_flags(self):
            return pl.DataFrame({"symbol": ["600000.SH"]})

    monkeypatch.setattr(backtest_api, "_get_data_provider", lambda capability: _MissingCols())
    assert backtest_api._strategy_listing_dates() is None


# ── /strategy/regime-breakdown ────────────────────────────


def _stub_curves(n_days: int = 150):
    d0 = date(2026, 1, 5)
    equity = [
        {"date": (d0 + timedelta(days=i)).isoformat(), "value": round(1.0 + 0.001 * i, 6)}
        for i in range(n_days)
    ]
    bench = [
        {"date": (d0 + timedelta(days=i)).isoformat(), "close": round(3000.0 + 2.0 * i, 4)}
        for i in range(n_days)
    ]
    return equity, bench


def _curve_service(monkeypatch, result: _StubResult):
    class _Service:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, cfg, progress_cb=None, cancel_event=None, **kwargs):
            return result

    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _Service)


def test_regime_breakdown_rejects_full_mode(client: TestClient, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("full 模式应在进入端点逻辑前被 422 拒绝")

    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _boom)
    resp = client.post("/api/backtest/strategy/regime-breakdown", json=_base_body(mode="full"))
    assert resp.status_code == 422
    assert "退出事件日采样" in resp.json()["detail"]


def test_regime_breakdown_shape_and_not_persisted(
    client: TestClient, tmp_path: Path, monkeypatch
):
    equity, bench = _stub_curves(150)
    _curve_service(monkeypatch, _StubResult(
        run_id="regime0001", equity_curve=equity, benchmark_curve=bench,
    ))

    resp = client.post("/api/backtest/strategy/regime-breakdown", json=_base_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"regime", "run_id", "note"}
    assert body["run_id"] == "regime0001"
    regime = body["regime"]
    assert regime is not None
    assert regime["n_days"] == 150
    assert regime["warmup_days"] == 59  # 60 日趋势窗口 warmup
    assert set(regime["buckets"]) == {
        "bull_turbulent", "bull_calm", "bear_turbulent", "bear_calm",
    }
    assert regime["definitions"]["trend"]
    assert regime["metric_context"]["return_frequency"] == "daily"
    # 诊断端点不持久化 Run
    assert not (tmp_path / "research" / "backtest_runs").exists()


def test_regime_breakdown_insufficient_alignment_returns_null_regime(
    client: TestClient, monkeypatch
):
    """对齐 < 120 天 → regime=None (fail-closed), 不伪造分桶。"""
    equity, bench = _stub_curves(60)
    _curve_service(monkeypatch, _StubResult(
        run_id="regime0002", equity_curve=equity, benchmark_curve=bench,
    ))
    resp = client.post("/api/backtest/strategy/regime-breakdown", json=_base_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["regime"] is None
    assert body["run_id"] == "regime0002"
    assert body["note"]


# ── /strategy/cost-sensitivity ────────────────────────────


def test_cost_sensitivity_rejects_bad_multipliers_before_engine(client: TestClient, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("非法倍数应在请求校验阶段被拒, 不触引擎")

    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _boom)
    # 负数
    assert client.post("/api/backtest/strategy/cost-sensitivity", json=_base_body(
        multipliers=[-0.5, 1.0]
    )).status_code == 422
    # 少于 2 档
    assert client.post("/api/backtest/strategy/cost-sensitivity", json=_base_body(
        multipliers=[1.0]
    )).status_code == 422
    # 多于 6 档
    assert client.post("/api/backtest/strategy/cost-sensitivity", json=_base_body(
        multipliers=[0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0]
    )).status_code == 422


def test_cost_sensitivity_rows_and_baseline_run_id(
    client: TestClient, tmp_path: Path, capturing_service, monkeypatch
):
    monkeypatch.setattr(backtest_api, "_strategy_listing_dates", lambda: None)
    resp = client.post("/api/backtest/strategy/cost-sensitivity", json=_base_body(
        multipliers=[3.0, 0.0, 1.0],  # 乱序输入 → 模块归一化升序
        max_participation_pct=0.1,
        min_listed_days=60,
    ))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 每档完整重跑一次: 3 档 3 次调用, cfg 透传新字段与缩放后的成本
    assert len(capturing_service.calls) == 3
    muls = body["cost_sensitivity"]["multipliers"]
    assert muls == [0.0, 1.0, 3.0]
    fees = [c["cfg"].fees_pct for c in capturing_service.calls]
    assert fees == [0.0002 * m for m in muls]
    assert all(c["cfg"].max_participation_pct == 0.1 for c in capturing_service.calls)
    assert all(c["kwargs"].get("listing_dates") is None for c in capturing_service.calls)

    rows = body["cost_sensitivity"]["rows"]
    assert [r["multiplier"] for r in rows] == muls
    baselines = [r for r in rows if r["is_baseline"]]
    assert len(baselines) == 1 and baselines[0]["multiplier"] == 1.0
    # 升序 1.0 排第二 → 基线 run_id 是第二次调用的结果
    assert body["run_id_baseline"] == "caprun002"
    assert isinstance(body["elapsed_ms"], (int, float))
    assert body["cost_sensitivity"]["note"]
    # 诊断端点不持久化
    assert not (tmp_path / "research" / "backtest_runs").exists()


# ── /strategy/style-attribution ───────────────────────────


def test_style_attribution_rejects_full_mode(client: TestClient, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("full 模式应在进入端点逻辑前被 422 拒绝")

    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", _boom)
    resp = client.post("/api/backtest/strategy/style-attribution", json=_base_body(mode="full"))
    assert resp.status_code == 422
    assert "退出事件日采样" in resp.json()["detail"]


def test_style_attribution_null_when_factors_insufficient(
    client: TestClient, monkeypatch
):
    """小面板 → 因子有效日不足 → style_attribution=null + meta 说明, 不伪造。"""
    engine: _StubEngine = client.app.state.backtest_engine
    engine.panel = pl.DataFrame({
        "date": [date(2026, 1, 5) + timedelta(days=i) for i in range(40)],
        "symbol": ["600000.SH"] * 40,
        "close": [10.0 + 0.1 * i for i in range(40)],
        "float_shares": [1e8] * 40,
    })
    equity, _ = _stub_curves(40)
    _curve_service(monkeypatch, _StubResult(run_id="style0001", equity_curve=equity))

    resp = client.post("/api/backtest/strategy/style-attribution", json=_base_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"style_attribution", "style_factor_meta", "run_id"}
    assert body["style_attribution"] is None
    assert body["run_id"] == "style0001"
    assert body["style_factor_meta"]["reason"] == "no_valid_cross_section"
    # 面板按该次回测 symbols 重建, 起点向前扩 470 自然日 warmup
    # (mom_252_21/vol_60 需要历史观测, 无 warmup 时 UMD 结构性缺失)
    assert engine.load_calls == [(["600000.SH"], date(2024, 9, 18), date(2026, 6, 30))]


# ── /runs/{run_id}/fill-reachability ──────────────────────


def _make_run(run_id: str = "fillrun001", *, kind: str = "strategy", trades: list) -> BacktestRun:
    return BacktestRun(
        run_id=run_id,
        kind=kind,
        stats={"sharpe": 1.0},
        config={"strategy_id": "macd"},
        equity_curve=[{"date": "2026-03-02", "value": 1.0}],
        trades=trades,
    )


def _trade(entry_date="2026-03-02", exit_date="2026-03-09") -> dict:
    return {
        "symbol": "600000.SH",
        "entry_date": entry_date,
        "entry_price": 10.0,
        "exit_date": exit_date,
        "exit_price": 10.2,
        "shares": 1000.0,
        "pnl_pct": 0.02,
    }


class _MinuteProvider:
    """固定分钟线桩: 10.0 附近三根在价格带内, 10.2 附近两根在 exit 带内。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple] = []

    def get_minute(self, symbols, start_time, end_time, asset_type="stock", **kw):
        self.calls.append((tuple(symbols), start_time, end_time))
        if self.fail:
            raise RuntimeError("minute source down")
        return pl.DataFrame({
            "close": [9.99, 10.0, 10.01, 10.2, 10.21],
            "amount": [1_000_000.0, 2_000_000.0, 1_500_000.0, 1_000_000.0, 1_000_000.0],
        })


def test_fill_reachability_run_not_found_404(client: TestClient):
    assert client.post("/api/backtest/runs/missing9/fill-reachability").status_code == 404


def test_fill_reachability_rejects_empty_trades_and_factor_kind(
    client: TestClient, tmp_path: Path
):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run("fillempty1", trades=[]))
    resp = client.post("/api/backtest/runs/fillempty1/fill-reachability")
    assert resp.status_code == 422
    assert "成交记录" in resp.json()["detail"]

    store.save(_make_run("fillfact01", kind="factor", trades=[_trade()]))
    resp = client.post("/api/backtest/runs/fillfact01/fill-reachability")
    assert resp.status_code == 422
    assert "factor" in resp.json()["detail"]


def test_fill_reachability_validates_sample_query(client: TestClient, tmp_path: Path):
    BacktestRunStore(tmp_path).save(_make_run(trades=[_trade()]))
    assert client.post(
        "/api/backtest/runs/fillrun001/fill-reachability", params={"sample": 0}
    ).status_code == 422
    assert client.post(
        "/api/backtest/runs/fillrun001/fill-reachability", params={"sample": 101}
    ).status_code == 422


def test_fill_reachability_ok_with_stub_minutes(client: TestClient, tmp_path: Path, monkeypatch):
    BacktestRunStore(tmp_path).save(_make_run(trades=[_trade()]))
    provider = _MinuteProvider()
    monkeypatch.setattr(backtest_api, "_get_data_provider", lambda capability: provider)

    resp = client.post(
        "/api/backtest/runs/fillrun001/fill-reachability",
        params={"sample": 20, "seed": 7},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"fill_reachability", "run_id"}
    assert body["run_id"] == "fillrun001"
    fill = body["fill_reachability"]
    assert fill["n_trades"] == 1
    assert fill["n_sampled"] == 1
    assert fill["sample_seed"] == 7
    assert fill["sides_checked"] == 2  # entry + exit
    assert fill["n_reachable"] == 1
    assert fill["headroom_p50"] is not None and fill["headroom_p50"] >= 1.0
    assert fill["note"]
    # 分钟请求按 (单 symbol, 单日 09:00-15:30, stock) 发出
    from datetime import datetime
    assert provider.calls == [
        (("600000.SH",), datetime(2026, 3, 2, 9, 0), datetime(2026, 3, 2, 15, 30)),
        (("600000.SH",), datetime(2026, 3, 9, 9, 0), datetime(2026, 3, 9, 15, 30)),
    ]


def test_fill_reachability_minute_failure_counts_no_data(
    client: TestClient, tmp_path: Path, monkeypatch
):
    """分钟源抛异常 → fail-soft 计入 no_data, 诊断仍 200 且不伪造可达性。"""
    BacktestRunStore(tmp_path).save(_make_run(trades=[_trade()]))
    provider = _MinuteProvider(fail=True)
    monkeypatch.setattr(backtest_api, "_get_data_provider", lambda capability: provider)

    resp = client.post("/api/backtest/runs/fillrun001/fill-reachability")
    assert resp.status_code == 200, resp.text
    fill = resp.json()["fill_reachability"]
    assert fill["n_sampled"] == 1
    assert fill["n_no_data"] == 1
    assert fill["n_reachable"] == 0
    assert fill["headroom_p50"] is None
    assert fill["worst"] == []


# ── A3 trade_equity_band (robustness 追加) ─────────────────


def test_robustness_trade_equity_band_present_when_enough_trades(
    client: TestClient, capturing_service
):
    capturing_service.result_trades = [
        {"pnl_pct": 0.01 + 0.001 * i} for i in range(12)
    ]
    resp = client.post("/api/backtest/strategy/robustness", json=_base_body(
        n_folds=2,
        parameter_perturbation=False,
        walk_forward_enabled=False,
        bootstrap=False,
        mc_permutation=False,
        seed=42,
    ))
    assert resp.status_code == 200, resp.text
    band = resp.json()["trade_equity_band"]
    assert band is not None
    assert set(band) == {
        "n_trades", "n_boot", "seed", "percentiles", "final_value_percentiles",
    }
    assert band["n_trades"] == 12
    assert band["n_boot"] == 1000
    assert band["seed"] == 42
    assert set(band["percentiles"]) == {"p05", "p25", "p50", "p75", "p95"}
    assert all(len(v) == 12 for v in band["percentiles"].values())
    assert set(band["final_value_percentiles"]) == {"p05", "p25", "p50", "p75", "p95"}

    # 持久化 Run 的 stats.robustness 带同一结构
    persisted = client.get(f"/api/backtest/runs/{resp.json()['run_id']}")
    assert persisted.status_code == 200
    persisted_band = persisted.json()["stats"]["robustness"]["trade_equity_band"]
    assert persisted_band == band


def test_robustness_trade_equity_band_none_when_trades_insufficient(
    client: TestClient, capturing_service
):
    capturing_service.result_trades = [{"pnl_pct": 0.01} for i in range(3)]
    resp = client.post("/api/backtest/strategy/robustness", json=_base_body(
        n_folds=2,
        parameter_perturbation=False,
        walk_forward_enabled=False,
        bootstrap=False,
        mc_permutation=False,
        seed=1,
    ))
    assert resp.status_code == 200, resp.text
    assert resp.json()["trade_equity_band"] is None
