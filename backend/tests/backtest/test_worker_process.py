from __future__ import annotations

import queue
import threading
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest import worker as worker_module
from app.backtest.mining import benchmark_candidate
from app.backtest.optimizer import OptimizeConfig
from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.walkforward import WalkForwardConfig
from app.backtest.worker import BacktestWorkerError, make_worker_task, run_worker_task
from app.enriched_generation import bump_enriched_generation, get_enriched_generation
from app.services.mining_jobs import MiningRunStore


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


def _write_mining_market_data(
    data_dir,
    start: date,
    *,
    days: int = 219,
    assets: int = 4,
) -> None:
    symbols = [f"60000{asset}.SH" for asset in range(assets)]
    for offset in range(days):
        current = start + timedelta(days=offset)
        rows = []
        for asset_id, symbol in enumerate(symbols):
            close = 10.0 + asset_id + offset * (0.01 + asset_id * 0.002)
            rows.append({
                "symbol": symbol,
                "date": current,
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1000.0 + asset_id * 100.0,
                "amount": close * (100000.0 + asset_id * 1000.0),
                "raw_close": close,
                "raw_high": close * 1.01,
                "raw_low": close * 0.99,
                "turnover_rate": 1.0 + asset_id * 0.5 + offset * 0.001,
                "consecutive_limit_ups": 0,
                "consecutive_limit_downs": 0,
            })
        partition = data_dir / "kline_daily_enriched" / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame(rows).write_parquet(partition / "part.parquet")

    instruments_dir = data_dir / "instruments"
    instruments_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": symbols,
        "name": [f"测试{asset}" for asset in range(assets)],
        "total_shares": [1_000_000_000.0] * assets,
        "float_shares": [1_000_000_000.0] * assets,
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


def test_spawn_mining_writes_four_artifacts_and_returns_compact_summary(tmp_path):
    start = date(2023, 1, 2)
    data_dir = tmp_path / "data"
    _write_mining_market_data(data_dir, start)
    store = MiningRunStore(data_dir)
    manifest = store.create(
        {
            "factor_names": ["turnover_rate"],
            "strategy_ids": [],
            "symbols": None,
            "asset_type": "stock",
            "start": (start - timedelta(days=7)).isoformat(),
            "end": (start + timedelta(days=225)).isoformat(),
            "budget_profile": "exploratory",
            "forward_horizon": 1,
            "commission_pct": 0.0,
            "stamp_tax_pct": 0.0,
            "slippage_bps": 0.0,
            "correlation_threshold": 0.75,
            "max_combination_factors": 1,
            "beam_width": 2,
            "max_finalists": 2,
            "require_regime": False,
        },
        {"generation": get_enriched_generation(data_dir, "stock")},
        run_id="spawn_mining",
    )
    payload = {
        "run_id": manifest["run_id"],
        "request": manifest["request"],
        "data_fingerprint": manifest["data_fingerprint"],
        "source": "manual",
    }

    result = run_worker_task(make_worker_task("mining", data_dir, payload))

    assert result["status"] in {"succeeded", "succeeded_with_budget_exhausted"}
    assert result["factor_count"] == 1
    assert result["data_as_of"] == (start + timedelta(days=218)).isoformat()
    assert result["panel_scans"] == 1
    assert result["matrix_bytes"] > 0
    assert result["worker"]["worker_exitcode"] == 0
    assert result["worker"]["serialized_result_bytes"] < 100_000
    registered = store.get("spawn_mining")["artifacts"]  # type: ignore[index]
    assert set(registered) == {"factors", "correlation", "candidates", "folds"}
    for name in registered:
        artifact = store.artifact_path("spawn_mining", name)
        assert artifact.is_file()
        frame = pl.read_parquet(artifact)
        assert frame.columns
        if name == "folds":
            assert "n_dates" in frame.columns
            assert frame.filter(pl.col("regime_state") == "overall")["n_dates"].min() > 0

def test_spawn_mining_benchmarks_strategy_on_every_outer_fold(tmp_path):
    start = date(2023, 1, 2)
    data_dir = tmp_path / "data"
    _write_mining_market_data(data_dir, start)
    store = MiningRunStore(data_dir)
    manifest = store.create(
        {
            "factor_names": ["turnover_rate"],
            "strategy_ids": ["low_volatility_leader"],
            "symbols": None,
            "asset_type": "stock",
            "start": (start - timedelta(days=7)).isoformat(),
            "end": (start + timedelta(days=225)).isoformat(),
            "budget_profile": "exploratory",
            "forward_horizon": 1,
            "commission_pct": 0.0,
            "stamp_tax_pct": 0.0,
            "slippage_bps": 0.0,
            "correlation_threshold": 0.75,
            "max_combination_factors": 1,
            "beam_width": 2,
            "max_finalists": 2,
            "require_regime": False,
        },
        {"generation": get_enriched_generation(data_dir, "stock")},
        run_id="spawn_mining_benchmark",
    )
    payload = {
        "run_id": manifest["run_id"],
        "request": manifest["request"],
        "data_fingerprint": manifest["data_fingerprint"],
        "source": "manual",
    }

    result = run_worker_task(make_worker_task("mining", data_dir, payload))

    assert result["status"] in {"succeeded", "succeeded_with_budget_exhausted"}
    folds = pl.read_parquet(store.artifact_path("spawn_mining_benchmark", "folds"))
    benchmark_signature = benchmark_candidate("low_volatility_leader").candidate_id
    benchmark_rows = folds.filter(
        (pl.col("evaluation_kind") == "benchmark")
        & (pl.col("candidate_signature") == benchmark_signature)
        & (pl.col("regime_state") == "overall")
    )
    outer_folds = result["valid_fold_count"] + result["skipped_fold_count"]
    assert outer_folds >= 1
    assert benchmark_rows.height == outer_folds
    selected_rows = folds.filter(
        (pl.col("evaluation_kind") == "selected")
        & (pl.col("regime_state") == "overall")
    )
    assert selected_rows.height == outer_folds
    candidates = pl.read_parquet(store.artifact_path("spawn_mining_benchmark", "candidates"))
    assert benchmark_signature in candidates["signature"].to_list()
    assert "existing_strategy" in candidates["kind"].to_list()


def test_spawn_mining_rejects_generation_change_after_queue(tmp_path):
    start = date(2023, 1, 2)
    data_dir = tmp_path / "data"
    _write_mining_market_data(data_dir, start)
    store = MiningRunStore(data_dir)
    queued_generation = get_enriched_generation(data_dir, "stock")
    manifest = store.create(
        {
            "factor_names": ["turnover_rate"],
            "strategy_ids": [],
            "symbols": None,
            "asset_type": "stock",
            "start": (start - timedelta(days=7)).isoformat(),
            "end": (start + timedelta(days=225)).isoformat(),
            "budget_profile": "exploratory",
            "forward_horizon": 1,
            "commission_pct": 0.0,
            "stamp_tax_pct": 0.0,
            "slippage_bps": 0.0,
            "correlation_threshold": 0.75,
            "max_combination_factors": 1,
            "beam_width": 2,
            "max_finalists": 2,
            "require_regime": False,
        },
        {"generation": queued_generation},
        run_id="stale_generation_mining",
    )
    bump_enriched_generation(data_dir, "stock")
    payload = {
        "run_id": manifest["run_id"],
        "request": manifest["request"],
        "data_fingerprint": manifest["data_fingerprint"],
        "source": "manual",
    }

    with pytest.raises(
        BacktestWorkerError,
        match="changed after the run was queued",
    ):
        run_worker_task(make_worker_task("mining", data_dir, payload))

    assert store.get("stale_generation_mining")["artifacts"] == {}  # type: ignore[index]


def test_worker_terminates_child_after_cancel_grace(monkeypatch, tmp_path):
    class FakeQueue:
        def get(self, timeout):
            raise queue.Empty

        def close(self):
            pass

        def join_thread(self):
            pass

    class FakeEvent:
        def set(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.exitcode = None

        def start(self):
            pass

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            pass

        def terminate(self):
            self.alive = False
            self.exitcode = -15

    process = FakeProcess()
    context = SimpleNamespace(
        Queue=FakeQueue,
        Event=FakeEvent,
        Process=lambda **_kwargs: process,
    )
    clock = iter([0.0, 0.0, 0.0, 6.0])
    monkeypatch.setattr(worker_module.mp, "get_context", lambda _method: context)
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: next(clock))
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(BacktestWorkerError, match="after cancellation"):
        run_worker_task(
            {"kind": "mining", "data_dir": str(tmp_path), "config": {}},
            cancel_event=cancel_event,
        )

    assert process.exitcode == -15


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


def _mining_runtime_services(data_dir):
    """按 worker._worker_entry 的方式在进程内构造挖掘运行时依赖 (便于 monkeypatch)。"""
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.strategy import config as strategy_config
    from app.strategy.engine import StrategyEngine
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(data_dir)
    repo = KlineRepository(store)
    strategy_engine = StrategyEngine(
        strategy_dirs=worker_module._strategy_dirs(data_dir),
        override_loader=lambda sid: strategy_config.load_override(data_dir, sid),
    )
    service = StrategyBacktestService(BacktestEngine(repo), strategy_engine)
    return service, strategy_engine


def _queue_mining_run(data_dir, start: date, run_id: str) -> dict:
    store = MiningRunStore(data_dir)
    manifest = store.create(
        {
            "factor_names": ["turnover_rate"],
            "strategy_ids": [],
            "symbols": None,
            "asset_type": "stock",
            "start": (start - timedelta(days=7)).isoformat(),
            "end": (start + timedelta(days=225)).isoformat(),
            "budget_profile": "exploratory",
            "forward_horizon": 1,
            "commission_pct": 0.0,
            "stamp_tax_pct": 0.0,
            "slippage_bps": 0.0,
            "correlation_threshold": 0.75,
            "max_combination_factors": 1,
            "beam_width": 2,
            "max_finalists": 2,
            "require_regime": False,
        },
        {"generation": get_enriched_generation(data_dir, "stock")},
        run_id=run_id,
    )
    return {
        "run_id": manifest["run_id"],
        "request": manifest["request"],
        "data_fingerprint": manifest["data_fingerprint"],
        "source": "manual",
    }


def _patch_generation_drift(monkeypatch, data_dir, *, always: bool) -> dict:
    """让世代校验第一次(或每次)调用前先 bump 再抛错, 模拟读取期间并发发布完成。"""
    from app.backtest.engine import BacktestEngine
    from app.enriched_generation import EnrichedGenerationUnavailableError

    original = BacktestEngine.assert_data_generation
    state = {"drifts": 0}

    def drift(engine_self, asset_type, expected):
        if expected is not None and (always or state["drifts"] == 0):
            state["drifts"] += 1
            bump_enriched_generation(data_dir, asset_type)
            raise EnrichedGenerationUnavailableError(
                "simulated concurrent publication"
            )
        return original(engine_self, asset_type, expected)

    monkeypatch.setattr(BacktestEngine, "assert_data_generation", drift)
    return state


def test_mining_rereads_snapshot_when_generation_commits_mid_read(
    monkeypatch, tmp_path
):
    start = date(2023, 1, 2)
    data_dir = tmp_path / "data"
    _write_mining_market_data(data_dir, start)
    payload = _queue_mining_run(data_dir, start, "midread_drift_mining")
    service, strategy_engine = _mining_runtime_services(data_dir)
    state = _patch_generation_drift(monkeypatch, data_dir, always=False)

    from app.backtest.mining_runtime import run_mining_runtime

    events: list[dict] = []
    result = run_mining_runtime(
        payload,
        data_dir=data_dir,
        service=service,
        strategy_engine=strategy_engine,
        progress_cb=events.append,
        cancel_check=None,
    )

    assert result["status"] in {"succeeded", "succeeded_with_budget_exhausted"}
    assert state["drifts"] == 1
    assert any(
        event.get("label") == "数据已更新, 重新读取快照" for event in events
    )


def test_mining_fails_with_guidance_when_generation_keeps_drifting(
    monkeypatch, tmp_path
):
    start = date(2023, 1, 2)
    data_dir = tmp_path / "data"
    _write_mining_market_data(data_dir, start)
    payload = _queue_mining_run(data_dir, start, "endless_drift_mining")
    service, strategy_engine = _mining_runtime_services(data_dir)
    _patch_generation_drift(monkeypatch, data_dir, always=True)

    from app.backtest.mining_runtime import run_mining_runtime

    with pytest.raises(ValueError, match="稳定快照"):
        run_mining_runtime(
            payload,
            data_dir=data_dir,
            service=service,
            strategy_engine=strategy_engine,
            progress_cb=None,
            cancel_check=None,
        )
