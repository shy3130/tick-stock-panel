"""守护复验运行时测试: 用假回测服务驱动完整判定落档链路。"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backtest.guardian_runtime import (
    GuardianRuntimeCancelledError,
    run_guardian_runtime,
)
from app.services.guardian_store import GuardianStore

_AS_OF = date(2025, 12, 31)


def _write_partitions(data_dir: Path, dates: list[date], asset_type: str = "stock") -> None:
    dirname = "kline_etf_enriched" if asset_type == "etf" else "kline_daily_enriched"
    for value in dates:
        partition = data_dir / dirname / f"date={value.isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        (partition / "part.parquet").write_bytes(b"")


def _trading_dates(count: int, first: date = date(2025, 1, 1)) -> list[date]:
    return [first + timedelta(days=index) for index in range(count)]


def _write_history(data_dir: Path) -> None:
    """406 个连续分区: 2025-01-01..2026-02-10 — 基线 (2025-12-31) 之后还有 41 天新数据。

    41 个自然日只够切 2 段验证窗 (k=2: 01-01..01-21 与 01-22..02-10)。
    """
    _write_partitions(data_dir, _trading_dates(406))


class _FakePrepared:
    def __init__(self) -> None:
        self.market_data = object()
        self.compute_cache = SimpleNamespace(close=lambda: None)


class _FakeService:
    """stats_by_start: {date.isoformat(): stats dict}; 默认调用返回 default。"""

    def __init__(self, default: dict, stats_by_start: dict | None = None,
                 errors_by_start: dict | None = None) -> None:
        self.default = default
        self.stats_by_start = stats_by_start or {}
        self.errors_by_start = errors_by_start or {}
        self.runs: list[date] = []
        self.prepared_calls = 0

    def prepare_matrix_optimization(self, configs, matrix_cache_max_bytes=0,
                                    market_data_override=None):
        self.prepared_calls += 1
        return _FakePrepared()

    def run(self, config, cancel_event=None, prepared=None, result_policy=None):
        self.runs.append(config.start)
        key = config.start.isoformat()
        if key in self.errors_by_start:
            return SimpleNamespace(stats={}, error=self.errors_by_start[key])
        stats = dict(self.stats_by_start.get(key, self.default))
        if result_policy is not None and result_policy.required_stats is not None:
            stats = {k: v for k, v in stats.items() if k in result_policy.required_stats}
        return SimpleNamespace(stats=stats, error=None)


class _FakeEngine:
    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known

    def get(self, strategy_id):
        if self.known is not None and strategy_id not in self.known:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return SimpleNamespace(
            meta={"asset_types": ["stock"], "name": strategy_id, "params": []},
            execution_backend="matrix_native",
        )

    def resolve_params(self, strategy, params=None, overrides=None):
        return {}


def _profile_store(tmp_path: Path) -> GuardianStore:
    return GuardianStore(tmp_path)


def _seed_mining_profile(store: GuardianStore, strategy_id: str = "mined_factor_x",
                         *, score: float = 2.0, n_trades: int = 300) -> None:
    store.create_baseline(
        strategy_id,
        kind="mining_gate",
        data_as_of=_AS_OF.isoformat(),
        objective="sharpe",
        metrics={"score": score, "n_trades": n_trades, "max_drawdown": -0.10},
    )


def _config(**overrides):
    params = {
        "run_id": "guardian-daily-test",
        "strategy_ids": None,
        "thresholds": {"watch_pct": 0.2, "retire_pct": 0.5, "k_max": 4},
    }
    params.update(overrides)
    return params


def test_healthy_check_transitions_pending_to_healthy(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store)
    service = _FakeService(
        default={"sharpe": 1.9, "n_trades": 120, "max_drawdown": -0.08}
    )

    result = run_guardian_runtime(
        _config(), data_dir=tmp_path, service=service, strategy_engine=_FakeEngine()
    )

    assert result["status"] == "succeeded"
    assert result["checked"] == 1 and result["errors"] == 0
    profile = store.get("mined_factor_x")
    assert profile["status"] == "healthy"
    check = profile["checks"][-1]
    assert check["verdict"] == "healthy"
    assert check["k"] == 2  # 41 个新自然日只够切 2 段
    assert len(check["segment_scores"]) == 2
    assert check["degradation_pct"] == pytest.approx(0.05)
    assert check["robust"]["score"] == pytest.approx(1.9)
    assert check["n_trades_total"] == 240
    # 超集矩阵 + 每段视图: 1 (full) + 2 (segments)
    assert service.prepared_calls == 3


def test_severe_degradation_retires(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store)
    service = _FakeService(default={"sharpe": 0.2, "n_trades": 150,
                                    "max_drawdown": -0.12})

    run_guardian_runtime(
        _config(), data_dir=tmp_path, service=service, strategy_engine=_FakeEngine()
    )

    profile = store.get("mined_factor_x")
    assert profile["status"] == "retired"
    assert "退化" in (profile["status_reason"] or "")


def test_retired_profile_not_rechecked(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store)
    store.set_status("mined_factor_x", "retired", reason="已退役")
    service = _FakeService(default={"sharpe": 1.9, "n_trades": 100})

    result = run_guardian_runtime(
        _config(), data_dir=tmp_path, service=service, strategy_engine=_FakeEngine()
    )

    assert result["skipped_retired"] == 1
    assert service.runs == []  # 退役策略不再复验
    assert len(store.get("mined_factor_x")["checks"]) == 0


def test_insufficient_new_data_skips_without_status_change(tmp_path):
    # 数据只到基线后 10 天
    dates = _trading_dates(50)[:41]  # 01-01..02-10; as_of=12-31 → 新数据 11 天
    _write_partitions(tmp_path, dates)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store)
    service = _FakeService(default={"sharpe": 1.0, "n_trades": 10})

    result = run_guardian_runtime(
        _config(), data_dir=tmp_path, service=service, strategy_engine=_FakeEngine()
    )

    assert result["skipped"] == 1
    profile = store.get("mined_factor_x")
    assert profile["status"] == "pending"
    assert profile["checks"][-1]["skipped"] is True
    assert "新数据" in profile["checks"][-1]["reason"]


def test_single_segment_error_yields_watch_not_retired(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store)
    from app.backtest.guardian import split_windows
    fresh_start, fresh_end = _AS_OF + timedelta(days=1), date(2026, 2, 10)
    first_segment_start = split_windows(fresh_start, fresh_end, 2)[0][0]
    service = _FakeService(
        default={"sharpe": 0.05, "n_trades": 150, "max_drawdown": -0.05},
        errors_by_start={first_segment_start.isoformat(): "boom"},
    )

    run_guardian_runtime(
        _config(), data_dir=tmp_path, service=service, strategy_engine=_FakeEngine()
    )

    profile = store.get("mined_factor_x")
    assert profile["status"] == "watch"
    check = profile["checks"][-1]
    assert check["verdict"] == "watch"
    assert check["incomplete"] is True
    assert any("验证段回测失败" in r for r in check["reasons"])
    # 残缺复验把首个段落级错误一并落档, 便于排查
    assert check["error"] == "boom"


def test_all_segments_error_records_error_and_keeps_status(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store)

    class _AlwaysError(_FakeService):
        def run(self, config, **kwargs):
            return SimpleNamespace(stats={}, error="db down")

    result = run_guardian_runtime(
        _config(), data_dir=tmp_path, service=_AlwaysError(default={}),
        strategy_engine=_FakeEngine(),
    )

    assert result["errors"] == 1
    profile = store.get("mined_factor_x")
    assert profile["status"] == "pending"
    assert "db down" in (profile["checks"][-1]["error"] or "")


def test_missing_strategy_records_error(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store, "ghost_strategy")

    result = run_guardian_runtime(
        _config(), data_dir=tmp_path, service=_FakeService(default={}),
        strategy_engine=_FakeEngine(known=set()),
    )

    assert result["errors"] == 1
    assert result["results"][0]["error"] == "strategy_unavailable"
    assert store.get("ghost_strategy")["status"] == "pending"


def test_trailing_reference_calibrates_then_checks(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    store.create_baseline(
        "my_manual_strategy",
        kind="trailing_reference",
        data_as_of=_AS_OF.isoformat(),
        objective="sharpe",
        metrics={},
    )
    service = _FakeService(default={"sharpe": 1.8, "n_trades": 90,
                                    "max_drawdown": -0.09})

    result = run_guardian_runtime(
        _config(), data_dir=tmp_path, service=service, strategy_engine=_FakeEngine()
    )

    assert result["checked"] == 1
    profile = store.get("my_manual_strategy")
    # 标定: 尾随 40 天窗口 (全部 100 天里最后 252→不足→全量 100... len=100≥30)
    assert profile["baseline"]["metrics"]["score"] == 1.8
    assert profile["baseline"]["metrics"]["window_start"] is not None
    assert profile["status"] == "healthy"


def test_cancel_raises(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(GuardianRuntimeCancelledError):
        run_guardian_runtime(
            _config(), data_dir=tmp_path, service=_FakeService(default={}),
            strategy_engine=_FakeEngine(), cancel_check=cancel,
        )


def test_strategy_ids_filter(tmp_path):
    _write_history(tmp_path)
    store = _profile_store(tmp_path)
    _seed_mining_profile(store, "mined_factor_a")
    _seed_mining_profile(store, "mined_factor_b")
    service = _FakeService(default={"sharpe": 1.9, "n_trades": 100})

    result = run_guardian_runtime(
        _config(strategy_ids=["mined_factor_b"]), data_dir=tmp_path,
        service=service, strategy_engine=_FakeEngine(),
    )

    assert result["checked"] == 1
    assert store.get("mined_factor_a")["status"] == "pending"
    assert store.get("mined_factor_b")["status"] == "healthy"
