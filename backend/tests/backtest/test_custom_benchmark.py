"""F9 自定义基准测试 — 个股/裸码指数标的基准、历史 Run 净值基准、互斥与降级。

service 级用桩引擎 (mock provider repo); API 级用 TestClient 挂 run_store
临时目录, 全部 422 校验发生在开跑前, 不触发行情读取。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.backtest.engine import SimResult
from app.backtest.run_store import BacktestRun, BacktestRunStore, RunSubject
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyDef


# ── 公共桩 ────────────────────────────────────────────────


def _strategy() -> StrategyDef:
    return StrategyDef(
        meta={"id": "test", "name": "test", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": True, "amount_min": 100.0},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=lambda df, params: pl.lit(True),
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
        file_path=None,
    )


class _StrategyEngineStub:
    def __init__(self, strategy: StrategyDef) -> None:
        self.strategy = strategy

    def get(self, strategy_id: str) -> StrategyDef:
        return self.strategy


class _Repo:
    """mock provider repo: 分别配置股票日K / 指数日K 返回。"""

    def __init__(
        self,
        stock_daily: pl.DataFrame | None = None,
        index_daily: pl.DataFrame | None = None,
    ) -> None:
        self.stock_daily = stock_daily if stock_daily is not None else pl.DataFrame()
        self.index_daily = index_daily if index_daily is not None else pl.DataFrame()
        self.daily_calls: list[tuple] = []
        self.index_calls: list[tuple] = []
    def get_daily(self, symbol, start, end, columns=None):
        self.daily_calls.append((symbol, start, end))
        return self.stock_daily

    def get_index_daily(self, symbol, start, end, columns=None):
        self.index_calls.append((symbol, start, end))
        return self.index_daily


def _panel() -> pl.DataFrame:
    rows = []
    for i in range(4):
        rows.append({
            "symbol": "A", "name": "A",
            "date": date(2024, 1, 2) + timedelta(days=i),
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "volume": 100_000, "amount": 1000.0,
            "signal_limit_up": False, "signal_limit_down": False,
        })
    return pl.DataFrame(rows).sort(["symbol", "date"])


class _NarrowEngine:
    """load_panel + simulate_portfolio 桩; equity 与 panel 日期一致。"""

    def __init__(self, repo: _Repo) -> None:
        self.repo = repo
        self.panel = _panel()

    def load_panel(self, symbols, start, end) -> pl.DataFrame:
        return self.panel

    def simulate_portfolio(self, panel, entries, exits, config, progress_cb=None, cancel_event=None) -> SimResult:
        dates = panel["date"].to_list()
        last = len(dates) - 1
        return SimResult(
            equity_curve=[
                {"date": str(d)[:10], "value": config.initial_capital * 1.1 if i == last else config.initial_capital}
                for i, d in enumerate(dates)
            ],
            drawdown_curve=[],
            trades=[],
            per_symbol_stats=[],
            stats={"total_return": 0.1, "n_trades": 1},
        )


def _service(repo: _Repo) -> StrategyBacktestService:
    return StrategyBacktestService(
        engine=_NarrowEngine(repo),
        strategy_engine=_StrategyEngineStub(_strategy()),
    )


def _cfg(**kwargs) -> StrategyBacktestConfig:
    defaults = dict(
        strategy_id="test", symbols=None,
        start=date(2024, 1, 1), end=date(2024, 1, 31),
        matching="close_t", mode="position",
    )
    defaults.update(kwargs)
    return StrategyBacktestConfig(**defaults)


def _daily_df(closes: list[float]) -> pl.DataFrame:
    base = date(2024, 1, 2)
    return pl.DataFrame({
        "date": [base + timedelta(days=i) for i in range(len(closes))],
        "close": closes,
    })


# ── service 级 ────────────────────────────────────────────


def test_custom_stock_benchmark_loads_from_get_daily():
    """非白名单个股代码走 provider.get_daily 单标的加载, source 标注 symbol。"""
    repo = _Repo(stock_daily=_daily_df([100.0, 102.0, 104.0, 110.0]))
    service = _service(repo)

    result = service.run(_cfg(benchmark_symbol="600519.SH"))

    assert result.error is None
    # 走股票路径 (get_daily), 且 symbol 原样传入
    assert repo.daily_calls and repo.daily_calls[0][0] == "600519.SH"
    assert not repo.index_calls
    assert result.stats["benchmark_source"] == {"kind": "symbol", "label": "600519.SH"}
    assert result.stats["benchmark_return"] == round(110.0 / 100.0 - 1, 4)
    assert result.benchmark_curve[0]["symbol"] == "600519.SH"


def test_bare_index_code_routes_to_index_daily():
    """裸码 399 开头按指数规则推断, canonical 为 .INDEX 后走 get_index_daily。"""
    repo = _Repo(index_daily=_daily_df([200.0, 202.0, 204.0, 210.0]))
    service = _service(repo)

    result = service.run(_cfg(benchmark_symbol="399006"))

    assert result.error is None
    assert repo.index_calls and repo.index_calls[0][0] == "399006.INDEX"
    assert not repo.daily_calls
    assert result.stats["benchmark_source"] == {"kind": "symbol", "label": "399006"}
    assert result.stats["benchmark_return"] == round(210.0 / 200.0 - 1, 4)


def test_run_benchmark_aligns_dates_and_computes_relative_metrics():
    """历史 Run 净值基准: 与策略净值按日期交集对齐, 复用相对指标管线。"""
    repo = _Repo()
    service = _service(repo)
    run_curve = [
        {"date": "2024-01-02", "value": 100.0},
        {"date": "2024-01-03", "value": 101.0},
        {"date": "2024-01-04", "value": 103.0},
        {"date": "2024-01-05", "value": 106.0},
    ]
    benchmark_run = {"run_id": "run00001", "label": "均线策略 A", "equity_curve": run_curve}

    result = service.run(_cfg(benchmark_run_id="run00001"), benchmark_run=benchmark_run)

    assert result.error is None
    assert result.stats["benchmark_source"] == {"kind": "run", "label": "均线策略 A"}
    assert result.stats["benchmark_return"] == round(106.0 / 100.0 - 1, 4)
    # 相对指标复用 relative_performance_metrics 管线 (3 个对齐收益对)
    assert result.stats["beta"] is not None and result.stats["beta"] > 0
    assert result.stats["alpha"] is not None
    assert result.benchmark_curve[0]["name"] == "均线策略 A"
    assert result.benchmark_curve[0]["symbol"] == "run00001"


def test_missing_custom_benchmark_degrades_with_warning():
    """基准加载落空 → 降级无基准 + warning 注明基准代码, 不伪造曲线。"""
    repo = _Repo(stock_daily=pl.DataFrame())
    service = _service(repo)

    result = service.run(_cfg(benchmark_symbol="300999.SZ"))

    assert result.error is None
    assert result.benchmark_curve == []
    assert result.stats["benchmark_source"] == {"kind": "none", "label": "300999.SZ"}
    assert any(
        w.startswith("benchmark_unavailable") and "300999.SZ" in w
        for w in result.warnings
    )


# ── API 级 (422 均发生在开跑前) ────────────────────────────


@pytest.fixture()
def api_client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = SimpleNamespace()
    app.state.backtest_engine = object()
    return TestClient(app)


def _run_equity(n: int = 40) -> list[dict]:
    base = date(2024, 1, 1)
    return [
        {"date": (base + timedelta(days=i)).isoformat(), "value": 100.0 + i}
        for i in range(n)
    ]


def _save_run(tmp_path: Path, run_id: str, equity: list[dict], stats: dict | None = None) -> None:
    BacktestRunStore(tmp_path).save(BacktestRun(
        run_id=run_id,
        kind="strategy",
        created_at="2024-03-01T00:00:00+00:00",
        subject=RunSubject(id="macd", name="MACD 策略", hash="h1"),
        config={"strategy_id": "macd", "start": "2024-01-01", "end": "2024-02-09"},
        stats=stats or {},
        equity_curve=equity,
    ))


def test_benchmark_symbol_and_run_id_are_mutually_exclusive(api_client):
    """同给 benchmark_symbol 与 benchmark_run_id → 422。"""
    res = api_client.post("/api/backtest/strategy/run", json={
        "strategy_id": "macd",
        "benchmark_symbol": "600519.SH",
        "benchmark_run_id": "run00001",
        "start": "2024-01-01", "end": "2024-06-30",
    })
    assert res.status_code == 422
    assert "互斥" in res.text


def test_benchmark_run_not_found_returns_422(api_client):
    res = api_client.post("/api/backtest/strategy/run", json={
        "strategy_id": "macd",
        "benchmark_run_id": "missing01",
        "start": "2024-01-01", "end": "2024-06-30",
    })
    assert res.status_code == 422
    assert "基准 run 不存在" in res.text


def test_benchmark_run_overlap_below_20_days_returns_422(api_client, tmp_path):
    """Run 净值与请求区间重叠交易日 < 20 → 422 带中文原因。"""
    _save_run(tmp_path, "run00002", _run_equity(40))
    res = api_client.post("/api/backtest/strategy/run", json={
        "strategy_id": "macd",
        "benchmark_run_id": "run00002",
        # Run 净值止于 2024-02-09, 请求 5 月起 → 重叠 0 天
        "start": "2024-05-01", "end": "2024-06-30",
    })
    assert res.status_code == 422
    assert "重叠交易日仅 0 天" in res.text


def test_benchmark_candidate_execution_run_rejected_422(api_client, tmp_path):
    """候选执行模式的 Run 无日频净值语义 → 422。"""
    _save_run(
        tmp_path, "run00003", _run_equity(40),
        stats={"full_kind": "candidate_execution"},
    )
    res = api_client.post("/api/backtest/strategy/run", json={
        "strategy_id": "macd",
        "benchmark_run_id": "run00003",
        "start": "2024-01-01", "end": "2024-06-30",
    })
    assert res.status_code == 422
    assert "候选执行" in res.text


def test_benchmark_factor_run_without_equity_rejected_422(api_client, tmp_path):
    """无日频净值的 Run (因子 run / 旧迁移) → 422。"""
    _save_run(tmp_path, "run00004", [])
    res = api_client.post("/api/backtest/strategy/run", json={
        "strategy_id": "macd",
        "benchmark_run_id": "run00004",
        "start": "2024-01-01", "end": "2024-06-30",
    })
    assert res.status_code == 422
    assert "无日频净值曲线" in res.text


def test_invalid_benchmark_symbol_format_returns_422(api_client):
    """基准代码格式非法 (非 6 位码/合法后缀) → 422。"""
    res = api_client.post("/api/backtest/strategy/run", json={
        "strategy_id": "macd",
        "benchmark_symbol": "not-a-code",
        "start": "2024-01-01", "end": "2024-06-30",
    })
    assert res.status_code == 422
    assert "benchmark_symbol" in res.text
