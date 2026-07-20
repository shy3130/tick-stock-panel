from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.backtest.optimizer import OptimizeConfig
from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.walkforward import WalkForwardConfig
from app.backtest.worker import make_worker_task, run_worker_task


def _write_worker_strategy(data_dir) -> None:
    strategy_dir = data_dir / "strategies" / "custom"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "worker_always_entry.py").write_text(
        """import numpy as np
from app.backtest.matrix import make_signal_matrix

META = {
    "id": "worker_always_entry",
    "name": "worker",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "gate", "type": "int", "default": 1, "min": 1, "max": 2, "step": 1},
    ],
    "scoring": {},
    "required_features": ["open", "high", "low", "close", "volume"],
}
EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = None
MAX_HOLD_DAYS = 1
ALERTS = []

class AlwaysEntry:
    def required_fields(self):
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params):
        return 1

    def compute_signals(self, market, params):
        return make_signal_matrix(
            market.shape,
            entry=np.ones(market.shape, dtype=np.uint8),
        )

MATRIX_STRATEGY = AlwaysEntry()
""",
        encoding="utf-8",
    )


def _write_market_data(data_dir, start: date, days: int = 3) -> None:
    rows = []
    for offset in range(days):
        close = 10.0 + offset
        current = start + timedelta(days=offset)
        rows.append({
            "symbol": "600000.SH",
            "date": current,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000.0,
            "amount": close * 100000.0,
            "raw_close": close,
            "raw_high": close,
            "raw_low": close,
            "turnover_rate": 1.0,
            "consecutive_limit_ups": 0,
            "consecutive_limit_downs": 0,
        })
        partition = data_dir / "kline_daily_enriched" / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame([rows[-1]]).write_parquet(partition / "part.parquet")

    instruments_dir = data_dir / "instruments"
    instruments_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "name": ["浦发银行"],
        "total_shares": [1_000_000_000.0],
        "float_shares": [1_000_000_000.0],
    }).write_parquet(instruments_dir / "part.parquet")


def test_spawn_worker_returns_compact_result_and_memory_metrics(tmp_path):
    start = date(2024, 1, 1)
    data_dir = tmp_path / "data"
    _write_worker_strategy(data_dir)
    _write_market_data(data_dir, start)
    config = StrategyBacktestConfig(
        strategy_id="worker_always_entry",
        symbols=["600000.SH"],
        start=start,
        end=start + timedelta(days=2),
        overrides={"basic_filter": {"enabled": False}},
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
    )

    result = run_worker_task(make_worker_task("backtest", data_dir, config))

    assert result["error"] is None
    assert result["stats"]["execution_backend"] == "matrix_native"
    worker = result["stats"]["worker"]
    assert worker["peak_rss_bytes"] > 0
    assert worker["serialized_result_bytes"] > 0
    assert worker["worker_exitcode"] == 0
    assert worker["parent_rss_after_worker_exit_bytes"] > 0


def test_spawn_optimizer_reuses_one_matrix_and_exits(tmp_path):
    start = date(2024, 1, 1)
    data_dir = tmp_path / "data"
    _write_worker_strategy(data_dir)
    _write_market_data(data_dir, start)
    config = OptimizeConfig(
        strategy_id="worker_always_entry",
        symbols=["600000.SH"],
        start=start,
        end=start + timedelta(days=2),
        param_grid={"gate": [1, 2]},
        objective="total_return",
        max_workers=4,
        overrides={"basic_filter": {"enabled": False}},
        backtest_kwargs={
            "matching": "close_t",
            "fees_pct": 0,
            "slippage_bps": 0,
            "max_positions": 1,
        },
    )

    result = run_worker_task(make_worker_task("optimize", data_dir, config))

    assert result["n_completed"] == 2
    assert result["effective_workers"] == 1
    assert result["shared_market_data"] is True
    assert result["shared_market_data_bytes"] > 0
    assert result["best_backtest"]["equity_curve"]
    assert result["best_backtest"]["trades"]
    assert "mc_maxdd_p50" in result["best_backtest"]["stats"]
    assert result["matrix_compute_cache"]["released"] is True
    assert result["performance"]["trial_peak_rss_bytes"] > 0
    assert result["performance"]["best_backtest_peak_rss_bytes"] > 0
    assert result["performance"]["trials_per_second"] > 0
    assert result["worker"]["worker_exitcode"] == 0


def test_spawn_walkforward_reuses_shared_matrix_across_folds(tmp_path):
    start = date(2024, 1, 1)
    data_dir = tmp_path / "data"
    _write_worker_strategy(data_dir)
    _write_market_data(data_dir, start, days=8)
    config = WalkForwardConfig(
        strategy_id="worker_always_entry",
        symbols=["600000.SH"],
        start=start,
        end=start + timedelta(days=7),
        param_grid={"gate": [1, 2]},
        objective="total_return",
        train_days=2,
        test_days=1,
        step_days=2,
        overrides={"basic_filter": {"enabled": False}},
        backtest_kwargs={
            "matching": "close_t",
            "fees_pct": 0,
            "slippage_bps": 0,
            "max_positions": 1,
        },
    )

    result = run_worker_task(make_worker_task("walkforward", data_dir, config))

    assert result["n_folds"] == 2
    assert result["shared_market_data"] is True
    assert result["shared_market_data_bytes"] > 0
    assert all(fold["oos_stats"]["shared_market_data"] for fold in result["folds"])
    assert result["worker"]["worker_exitcode"] == 0


def test_spawn_walkforward_skips_folds_before_available_matrix_data(tmp_path):
    configured_start = date(2024, 1, 1)
    market_start = configured_start + timedelta(days=4)
    data_dir = tmp_path / "data"
    _write_worker_strategy(data_dir)
    _write_market_data(data_dir, market_start, days=8)
    config = WalkForwardConfig(
        strategy_id="worker_always_entry",
        symbols=["600000.SH"],
        start=configured_start,
        end=configured_start + timedelta(days=11),
        param_grid={"gate": [1, 2]},
        objective="total_return",
        train_days=2,
        test_days=1,
        step_days=2,
        overrides={"basic_filter": {"enabled": False}},
        backtest_kwargs={
            "matching": "close_t",
            "fees_pct": 0,
            "slippage_bps": 0,
            "max_positions": 1,
        },
    )

    result = run_worker_task(make_worker_task("walkforward", data_dir, config))

    assert result["n_planned_folds"] == 4
    assert result["n_skipped"] == 1
    assert result["skipped"][0]["reason"] == "训练区间无可用行情数据"
    assert result["n_folds"] == 3
    assert result["worker"]["worker_exitcode"] == 0
