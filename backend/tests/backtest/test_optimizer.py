"""策略寻优纯逻辑测试 — 不依赖真实行情。"""
from __future__ import annotations

import threading
from datetime import date

import numpy as np
import pytest

from app.backtest.optimizer import (
    HARD_MAX_SCENARIOS,
    MAX_COMBO_STRATEGIES,
    PER_SYMBOL_MAX,
    UniverseSpec,
    SearchExperiment,
    SearchScenarioResult,
    build_universes,
    calendar_phases,
    classify_board,
    combine_equal_weight,
    cscv_pbo,
    deflated_sharpe_ratio,
    eligible_for_holdout,
    equity_to_returns,
    expand_combo_specs,
    expand_search_scenarios,
    expected_max_sharpe,
    install_combo_strategies,
    leaf_strategy_ids,
    OptimizerExperimentStore,
    passes_constraints,
    phase_returns,
    resolve_window,
    run_search,
    split_train_holdout,
)
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestResult


def test_classify_board():
    assert classify_board("600000.SH") == "main"
    assert classify_board("000001.SZ") == "main"
    assert classify_board("300750.SZ") == "gem"
    assert classify_board("688981.SH") == "star"
    assert classify_board("920001.BJ") == "bj"
    assert classify_board("") is None


def test_split_train_holdout_no_overlap():
    train_start, train_end, holdout_start, holdout_end = split_train_holdout(
        date(2018, 8, 14), date(2026, 8, 14),
    )
    assert train_start == date(2018, 8, 14)
    assert holdout_end == date(2026, 8, 14)
    assert train_end < holdout_start
    assert (holdout_start - train_end).days == 1
    train_span = (train_end - train_start).days
    total = (holdout_end - train_start).days
    assert 0.7 < train_span / total < 0.8


def test_split_rejects_short_window():
    with pytest.raises(ValueError, match="60"):
        split_train_holdout(date(2026, 1, 1), date(2026, 1, 20))


def test_resolve_window_clamps_to_data_bounds():
    start, end, warnings = resolve_window(
        end=date(2026, 8, 20),
        years=8,
        earliest=date(2020, 1, 1),
        latest=date(2026, 8, 14),
    )
    assert end == date(2026, 8, 14)
    assert start == date(2020, 1, 1)
    assert "end_clamped_to_latest" in warnings
    assert "start_clamped_to_earliest" in warnings


def test_calendar_phases_cover_full_span():
    phases = calendar_phases(date(2024, 6, 1), date(2026, 3, 1))
    assert [p["id"] for p in phases] == ["2024", "2025", "2026"]
    assert phases[0]["start"] == "2024-06-01"
    assert phases[-1]["end"] == "2026-03-01"


def test_build_universes_boards_and_custom():
    universes = build_universes(
        symbols=["600000.SH", "300750.SZ", "688981.SH"],
        include_all_a=True,
        boards=["main", "gem"],
        industries=[],
        industry_map=None,
        per_symbol=False,
        industry_top_n=0,
    )
    ids = [u.universe_id for u in universes]
    assert ids[0] == "all_a"
    assert "board:main" in ids
    assert "board:gem" in ids
    custom = next(u for u in universes if u.kind == "symbols")
    assert custom.symbols == ("600000.SH", "300750.SZ", "688981.SH")


def test_build_universes_industry_top_n():
    imap = {
        "600000.SH": ["银行"],
        "601398.SH": ["银行"],
        "600036.SH": ["银行"],
        "000001.SZ": ["银行"],
        "300750.SZ": ["电力设备"],
        "300014.SZ": ["电力设备"],
        "600519.SH": ["食品饮料"],
    }
    universes = build_universes(
        symbols=None,
        include_all_a=False,
        boards=[],
        industries=[],
        industry_map=imap,
        per_symbol=False,
        industry_top_n=2,
    )
    assert [u.universe_id for u in universes] == ["industry:银行", "industry:电力设备"]
    assert len(universes[0].symbols) == 4


def test_per_symbol_requires_small_list():
    with pytest.raises(ValueError, match="个股"):
        build_universes(
            symbols=[f"00000{i}.SZ" for i in range(PER_SYMBOL_MAX + 1)],
            include_all_a=False,
            boards=[],
            industries=[],
            industry_map=None,
            per_symbol=True,
            industry_top_n=0,
        )


def test_expand_is_deterministic_when_truncated():
    universes = [UniverseSpec(f"u{i}", f"U{i}", "all_a", None) for i in range(20)]
    base = StrategyBacktestConfig(strategy_id="x", symbols=None, start=date(2020, 1, 1), end=date(2021, 1, 1))
    a, requested, truncated = expand_search_scenarios(
        strategy_ids=["s1", "s2"],
        universes=universes,
        holding_days=[5, 10, 20],
        matchings=["open_t+1"],
        base=base,
        max_scenarios=10,
        seed=7,
    )
    b, _, _ = expand_search_scenarios(
        strategy_ids=["s1", "s2"],
        universes=universes,
        holding_days=[5, 10, 20],
        matchings=["open_t+1"],
        base=base,
        max_scenarios=10,
        seed=7,
    )
    assert requested == 120
    assert truncated
    assert [s.scenario_id for s in a] == [s.scenario_id for s in b]
    assert len(a) == 10


def test_expand_combo_specs_pairwise_and_capped():
    assert expand_combo_specs(["only"]) == []
    pairs = expand_combo_specs(["b", "a", "c"])
    assert len(pairs) == 3
    assert all(item[0].startswith("combo:") for item in pairs)
    assert {item[1] for item in pairs} == {("a", "b"), ("a", "c"), ("b", "c")}
    many = [f"s{i:02d}" for i in range(20)]
    capped = expand_combo_specs(many)
    assert len(capped) == MAX_COMBO_STRATEGIES
    again = expand_combo_specs(many)
    assert [item[0] for item in again] == [item[0] for item in capped]


def test_leaf_strategy_ids_skip_composite_and_ai():
    class _Eng:
        def get(self, sid):
            table = {
                "a": type("S", (), {"execution_backend": "polars_expr", "source": "builtin", "ephemeral": False})(),
                "b": type("S", (), {"execution_backend": "composite", "source": "composite", "ephemeral": False})(),
                "c": type("S", (), {"execution_backend": "polars_expr", "source": "ai", "ephemeral": False})(),
            }
            return table[sid]
    assert leaf_strategy_ids(_Eng(), ["a", "b", "c", "a"]) == ["a"]


def test_install_combo_strategies_uses_ephemeral_hook():
    installed: dict[str, object] = {}

    class _Eng:
        def put_ephemeral(self, sid, spec):
            installed[sid] = spec

    specs = expand_combo_specs(["ma_golden_cross", "trend_breakout"])
    ids = install_combo_strategies(_Eng(), specs)
    assert len(ids) == 1
    spec = installed[ids[0]]
    assert spec.ephemeral is True
    assert spec.file_path is None
    assert spec.execution_backend == "composite"
    assert spec.composite.children[0].strategy_id == "ma_golden_cross"


def test_hard_cap_not_exceeded():
    universes = [UniverseSpec(f"u{i}", f"U{i}", "all_a", None) for i in range(50)]
    base = StrategyBacktestConfig(strategy_id="x", symbols=None, start=date(2020, 1, 1), end=date(2021, 1, 1))
    scenarios, requested, truncated = expand_search_scenarios(
        strategy_ids=["a", "b", "c", "d"],
        universes=universes,
        holding_days=[5, 10, 20],
        matchings=["open_t+1", "close_t"],
        base=base,
        max_scenarios=10_000,
    )
    assert requested > HARD_MAX_SCENARIOS
    assert truncated
    assert len(scenarios) == HARD_MAX_SCENARIOS


def test_dsr_falls_as_trials_increase():
    one = deflated_sharpe_ratio(0.08, n_trials=1, n_obs=500, skew=0.0, kurtosis=3.0)
    many = deflated_sharpe_ratio(0.08, n_trials=80, n_obs=500, skew=0.0, kurtosis=3.0)
    assert one is not None and many is not None
    assert one > many
    assert 0.0 < many < 1.0
    assert expected_max_sharpe(1) == 0.0
    assert expected_max_sharpe(100) > expected_max_sharpe(10)


def test_pbo_high_when_best_in_sample_fails_out_of_sample():
    rng = np.random.default_rng(0)
    noise = [rng.normal(0.0, 0.01, size=160) for _ in range(5)]
    trap = np.concatenate([np.full(80, 0.02), np.full(80, -0.02)])
    pbo = cscv_pbo(noise + [trap], n_blocks=8)
    assert pbo["pbo"] is not None
    assert pbo["n_combinations"] == 70
    assert pbo["pbo"] >= 0.3


def test_pbo_low_when_one_series_consistently_better():
    rng = np.random.default_rng(1)
    noise = [rng.normal(0.0, 0.01, size=160) for _ in range(5)]
    winner = rng.normal(0.02, 0.01, size=160)
    pbo = cscv_pbo(noise + [winner], n_blocks=8)
    assert pbo["pbo"] is not None
    assert pbo["pbo"] <= 0.35


def test_passes_constraints_and_phase_returns():
    assert passes_constraints(
        {"n_trades": 20, "total_return": 0.1, "max_drawdown": -0.05, "pending_exit_positions": 0},
        min_trades=10,
        max_drawdown=0.2,
    )
    assert not passes_constraints(
        {"n_trades": 20, "total_return": -0.01, "max_drawdown": -0.05},
        min_trades=10,
        max_drawdown=None,
    )
    assert eligible_for_holdout(
        {"n_trades": 20, "total_return": -0.01, "max_drawdown": -0.05, "pending_exit_positions": 0},
        min_trades=10,
        max_drawdown=None,
    )
    assert not eligible_for_holdout(
        {"n_trades": 2, "total_return": 0.5, "pending_exit_positions": 0},
        min_trades=10,
        max_drawdown=None,
    )
    curve = [
        {"date": "2024-01-02", "value": 1.0},
        {"date": "2024-06-28", "value": 1.1},
        {"date": "2025-01-02", "value": 1.2},
        {"date": "2025-12-30", "value": 0.9},
    ]
    phases = calendar_phases(date(2024, 1, 1), date(2025, 12, 31))
    rows = phase_returns(curve, phases)
    assert rows[0]["total_return"] == pytest.approx(0.1, abs=1e-6)
    assert rows[1]["total_return"] == pytest.approx(0.9 / 1.2 - 1.0, abs=1e-6)


def test_equal_weight_combo_averages_normalized_curves():
    a = [{"date": "2024-01-01", "value": 100.0}, {"date": "2024-01-02", "value": 110.0}]
    b = [{"date": "2024-01-01", "value": 50.0}, {"date": "2024-01-02", "value": 50.0}]
    combo = combine_equal_weight([a, b])
    assert combo[0]["value"] == pytest.approx(1.0)
    assert combo[1]["value"] == pytest.approx((1.1 + 1.0) / 2)


class _ScriptedService:
    def __init__(self, table: dict[tuple, dict]) -> None:
        self.table = table
        self.calls: list[tuple] = []

    def run(self, config, progress_cb=None, cancel_event=None, panel=None):
        key = (config.strategy_id, config.start, config.end, config.holding_days)
        self.calls.append(key)
        stats = self.table.get(key)
        if stats is None:
            return StrategyBacktestResult(run_id="x", config={}, error="missing")
        start_v = 1.0
        end_v = 1.0 + float(stats.get("total_return") or 0.0)
        n = 40
        curve = [
            {"date": (config.start if i == 0 else config.end).isoformat(), "value": start_v + (end_v - start_v) * i / (n - 1)}
            for i in range(n)
        ]
        return StrategyBacktestResult(
            run_id="x",
            config={},
            stats=stats,
            equity_curve=curve,
            elapsed_ms=1.0,
        )


def test_run_search_ranks_on_train_and_admits_on_holdout(tmp_path):
    train_start, train_end = date(2020, 1, 1), date(2024, 12, 31)
    holdout_start, holdout_end = date(2025, 1, 1), date(2026, 8, 14)
    table = {
        ("good", train_start, train_end, 5): {
            "sharpe": 1.2, "total_return": 0.4, "max_drawdown": -0.1, "n_trades": 40, "pending_exit_positions": 0,
        },
        ("good", holdout_start, holdout_end, 5): {
            "sharpe": 0.8, "total_return": 0.12, "max_drawdown": -0.08, "n_trades": 12, "pending_exit_positions": 0,
        },
        ("trap", train_start, train_end, 5): {
            "sharpe": 2.5, "total_return": 0.9, "max_drawdown": -0.05, "n_trades": 50, "pending_exit_positions": 0,
        },
        ("trap", holdout_start, holdout_end, 5): {
            "sharpe": -0.4, "total_return": -0.2, "max_drawdown": -0.3, "n_trades": 12, "pending_exit_positions": 0,
        },
    }
    universes = [UniverseSpec("all_a", "全A", "all_a", None)]
    base = StrategyBacktestConfig(strategy_id="good", symbols=None, start=train_start, end=holdout_end)
    scenarios, requested, truncated = expand_search_scenarios(
        strategy_ids=["trap", "good"],
        universes=universes,
        holding_days=[5],
        matchings=["open_t+1"],
        base=base,
    )
    store = OptimizerExperimentStore(tmp_path)
    exp = run_search(
        _ScriptedService(table),
        store,
        experiment_id="so-deadbeef0001",
        config_hash="abc",
        objective="sharpe",
        scenarios=scenarios,
        requested_count=requested,
        truncated=truncated,
        train_start=train_start,
        train_end=train_end,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        min_trades=10,
        top_k=2,
    )
    assert exp.status == "completed"
    assert exp.scenarios[0].strategy_id == "trap"
    assert exp.scenarios[0].admitted is False
    good = next(s for s in exp.scenarios if s.strategy_id == "good")
    assert good.admitted is True
    assert exp.recommended_ids == [good.scenario_id]
    assert good.holdout_stats is not None
    assert exp.runtime["stage"] == "completed"
    assert exp.runtime["ok"] >= 1
    assert "current" in exp.runtime
    loaded = store.load("so-deadbeef0001")
    assert loaded.recommended_ids == exp.recommended_ids
    assert loaded.diagnostics["pbo"]["n_trials"] >= 1
    assert loaded.runtime["stage"] == "completed"


def test_holdout_not_used_for_ranking(tmp_path):
    train_start, train_end = date(2020, 1, 1), date(2024, 12, 31)
    holdout_start, holdout_end = date(2025, 1, 1), date(2026, 8, 14)
    table = {
        ("low_train", train_start, train_end, 5): {
            "sharpe": 0.2, "total_return": 0.05, "max_drawdown": -0.1, "n_trades": 20, "pending_exit_positions": 0,
        },
        ("low_train", holdout_start, holdout_end, 5): {
            "sharpe": 3.0, "total_return": 0.8, "max_drawdown": -0.02, "n_trades": 20, "pending_exit_positions": 0,
        },
        ("high_train", train_start, train_end, 5): {
            "sharpe": 1.0, "total_return": 0.3, "max_drawdown": -0.1, "n_trades": 20, "pending_exit_positions": 0,
        },
        ("high_train", holdout_start, holdout_end, 5): {
            "sharpe": 0.1, "total_return": 0.02, "max_drawdown": -0.1, "n_trades": 20, "pending_exit_positions": 0,
        },
    }
    universes = [UniverseSpec("all_a", "全A", "all_a", None)]
    base = StrategyBacktestConfig(strategy_id="x", symbols=None, start=train_start, end=holdout_end)
    scenarios, requested, truncated = expand_search_scenarios(
        strategy_ids=["low_train", "high_train"],
        universes=universes,
        holding_days=[5],
        matchings=["open_t+1"],
        base=base,
    )
    exp = run_search(
        _ScriptedService(table),
        OptimizerExperimentStore(tmp_path),
        experiment_id="so-cafe00000001",
        config_hash="h",
        objective="sharpe",
        scenarios=scenarios,
        requested_count=requested,
        truncated=truncated,
        train_start=train_start,
        train_end=train_end,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
    )
    assert exp.scenarios[0].strategy_id == "high_train"
    assert exp.scenarios[0].rank == 1



def test_negative_train_still_runs_holdout(tmp_path):
    train_start, train_end = date(2020, 1, 1), date(2024, 12, 31)
    holdout_start, holdout_end = date(2025, 1, 1), date(2026, 8, 14)
    table = {
        ("loser", train_start, train_end, 5): {
            "sharpe": -0.2, "total_return": -0.05, "max_drawdown": -0.1, "n_trades": 20, "pending_exit_positions": 0,
        },
        ("loser", holdout_start, holdout_end, 5): {
            "sharpe": 0.6, "total_return": 0.1, "max_drawdown": -0.05, "n_trades": 12, "pending_exit_positions": 0,
        },
    }
    universes = [UniverseSpec("all_a", "全A", "all_a", None)]
    base = StrategyBacktestConfig(strategy_id="loser", symbols=None, start=train_start, end=holdout_end)
    scenarios, requested, truncated = expand_search_scenarios(
        strategy_ids=["loser"],
        universes=universes,
        holding_days=[5],
        matchings=["open_t+1"],
        base=base,
    )
    exp = run_search(
        _ScriptedService(table),
        OptimizerExperimentStore(tmp_path),
        experiment_id="so-aaaaaaaa0001",
        config_hash="n",
        objective="sharpe",
        scenarios=scenarios,
        requested_count=requested,
        truncated=truncated,
        train_start=train_start,
        train_end=train_end,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        min_trades=10,
    )
    assert exp.scenarios[0].holdout_stats is not None
    assert exp.scenarios[0].admitted is True
    assert exp.recommended_ids == [exp.scenarios[0].scenario_id]


def test_equity_to_returns():
    rets = equity_to_returns([
        {"date": "a", "value": 100},
        {"date": "b", "value": 110},
        {"date": "c", "value": 99},
    ])
    assert rets[0] == pytest.approx(0.1)
    assert rets[1] == pytest.approx(99 / 110 - 1)


def test_from_dict_old_file_without_request_defaults_none():
    exp = SearchExperiment.from_dict({
        "experiment_id": "so-old00000001",
        "config_hash": "h",
        "objective": "sharpe",
        "start": "2020-01-01",
        "end": "2026-08-14",
        "train_end": "2024-12-31",
        "holdout_start": "2025-01-01",
        "requested_count": 1,
        "scenario_count": 1,
        "max_scenarios": 1,
        "truncated": False,
        "status": "interrupted",
        "unknown_future_field": {"x": 1},
    })
    assert exp.request is None
    assert exp.status == "interrupted"


def _resume_setup(strategy_ids: list[str]):
    train_start, train_end = date(2020, 1, 1), date(2024, 12, 31)
    holdout_start, holdout_end = date(2025, 1, 1), date(2026, 8, 14)
    universes = [UniverseSpec("all_a", "全A", "all_a", None)]
    base = StrategyBacktestConfig(strategy_id=strategy_ids[0], symbols=None, start=train_start, end=holdout_end)
    scenarios, requested, truncated = expand_search_scenarios(
        strategy_ids=strategy_ids,
        universes=universes,
        holding_days=[5],
        matchings=["open_t+1"],
        base=base,
    )
    return scenarios, requested, truncated, train_start, train_end, holdout_start, holdout_end


def test_run_search_resume_skips_completed_train_and_holdout(tmp_path):
    scenarios, requested, truncated, train_start, train_end, holdout_start, holdout_end = _resume_setup(["keep", "todo"])
    sid_keep = next(s.scenario_id for s in scenarios if s.strategy_id == "keep")
    done = SearchScenarioResult(
        scenario_id=sid_keep,
        strategy_id="keep",
        universe_id="all_a",
        universe_label="全A",
        universe_kind="all_a",
        holding_days=5,
        matching="open_t+1",
        train_stats={"sharpe": 1.2, "total_return": 0.4, "max_drawdown": -0.1, "n_trades": 40, "pending_exit_positions": 0},
        holdout_stats={"sharpe": 0.8, "total_return": 0.12, "max_drawdown": -0.08, "n_trades": 12, "pending_exit_positions": 0},
        score=1.2,
        admitted=True,
    )
    existing = SearchExperiment(
        experiment_id="so-0aaa00000001",
        config_hash="h1",
        objective="sharpe",
        start=train_start.isoformat(),
        end=holdout_end.isoformat(),
        train_end=train_end.isoformat(),
        holdout_start=holdout_start.isoformat(),
        requested_count=requested,
        scenario_count=len(scenarios),
        max_scenarios=len(scenarios),
        truncated=truncated,
        status="interrupted",
        scenarios=[done],
        created_at="2026-01-01T00:00:00+00:00",
        request={"strategy_ids": ["keep", "todo"]},
    )
    table = {
        ("todo", train_start, train_end, 5): {
            "sharpe": 0.5, "total_return": 0.1, "max_drawdown": -0.1, "n_trades": 20, "pending_exit_positions": 0,
        },
    }
    svc = _ScriptedService(table)
    store = OptimizerExperimentStore(tmp_path)
    exp = run_search(
        svc,
        store,
        experiment_id="so-0aaa00000001",
        config_hash="h1",
        objective="sharpe",
        scenarios=scenarios,
        requested_count=requested,
        truncated=truncated,
        train_start=train_start,
        train_end=train_end,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        min_trades=10,
        top_k=1,
        existing=existing,
    )
    # 只补跑缺失训练的 todo；keep 的训练与留出都不重跑
    assert svc.calls == [("todo", train_start, train_end, 5)]
    assert exp.status == "completed"
    assert "resumed_after_interrupt" in exp.warnings
    # 缺本进程训练曲线：DSR/PBO 置空并标记，不伪造
    assert "resumed_partial_diagnostics" in exp.warnings
    assert exp.diagnostics["dsr"] is None
    assert exp.diagnostics["pbo"] is None
    # created_at 与 request 快照保留
    assert exp.created_at == "2026-01-01T00:00:00+00:00"
    assert exp.request == {"strategy_ids": ["keep", "todo"]}
    kept = next(s for s in exp.scenarios if s.strategy_id == "keep")
    assert kept.holdout_stats["total_return"] == 0.12
    assert exp.recommended_ids == [sid_keep]


def test_run_search_resume_reruns_missing_holdout(tmp_path):
    scenarios, requested, truncated, train_start, train_end, holdout_start, holdout_end = _resume_setup(["keep"])
    sid_keep = next(s.scenario_id for s in scenarios if s.strategy_id == "keep")
    done = SearchScenarioResult(
        scenario_id=sid_keep,
        strategy_id="keep",
        universe_id="all_a",
        universe_label="全A",
        universe_kind="all_a",
        holding_days=5,
        matching="open_t+1",
        train_stats={"sharpe": 1.2, "total_return": 0.4, "max_drawdown": -0.1, "n_trades": 40, "pending_exit_positions": 0},
        score=1.2,
        holdout_stats=None,
    )
    existing = SearchExperiment(
        experiment_id="so-0aaa00000002",
        config_hash="h2",
        objective="sharpe",
        start=train_start.isoformat(),
        end=holdout_end.isoformat(),
        train_end=train_end.isoformat(),
        holdout_start=holdout_start.isoformat(),
        requested_count=requested,
        scenario_count=len(scenarios),
        max_scenarios=len(scenarios),
        truncated=truncated,
        status="interrupted",
        scenarios=[done],
        request={"strategy_ids": ["keep"]},
    )
    table = {
        ("keep", holdout_start, holdout_end, 5): {
            "sharpe": 0.9, "total_return": 0.15, "max_drawdown": -0.08, "n_trades": 12, "pending_exit_positions": 0,
        },
    }
    svc = _ScriptedService(table)
    exp = run_search(
        svc,
        OptimizerExperimentStore(tmp_path),
        experiment_id="so-0aaa00000002",
        config_hash="h2",
        objective="sharpe",
        scenarios=scenarios,
        requested_count=requested,
        truncated=truncated,
        train_start=train_start,
        train_end=train_end,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        min_trades=10,
        top_k=1,
        existing=existing,
    )
    # 训练不重跑，只补缺失的留出
    assert svc.calls == [("keep", holdout_start, holdout_end, 5)]
    assert exp.status == "completed"
    kept = exp.scenarios[0]
    assert kept.holdout_stats is not None
    assert kept.admitted is True
    assert "resumed_after_interrupt" in exp.warnings
    assert exp.diagnostics["dsr"] is None
    assert "resumed_partial_diagnostics" in exp.warnings


def test_run_search_cancel_during_resume_writes_cancelled(tmp_path):
    scenarios, requested, truncated, train_start, train_end, holdout_start, holdout_end = _resume_setup(["keep"])
    cancel_event = threading.Event()
    svc = _ScriptedService({})
    existing = SearchExperiment(
        experiment_id="so-0aaa00000003",
        config_hash="h3",
        objective="sharpe",
        start=train_start.isoformat(),
        end=holdout_end.isoformat(),
        train_end=train_end.isoformat(),
        holdout_start=holdout_start.isoformat(),
        requested_count=requested,
        scenario_count=len(scenarios),
        max_scenarios=len(scenarios),
        truncated=truncated,
        status="interrupted",
        request={"strategy_ids": ["keep"]},
    )
    cancel_event.set()
    exp = run_search(
        svc,
        OptimizerExperimentStore(tmp_path),
        experiment_id="so-0aaa00000003",
        config_hash="h3",
        objective="sharpe",
        scenarios=scenarios,
        requested_count=requested,
        truncated=truncated,
        train_start=train_start,
        train_end=train_end,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        cancel_event=cancel_event,
        existing=existing,
    )
    assert exp.status == "cancelled"
    assert svc.calls == []


def test_run_search_fresh_persists_request_snapshot(tmp_path):
    scenarios, requested, truncated, train_start, train_end, holdout_start, holdout_end = _resume_setup(["keep"])
    table = {
        ("keep", train_start, train_end, 5): {
            "sharpe": 1.2, "total_return": 0.4, "max_drawdown": -0.1, "n_trades": 40, "pending_exit_positions": 0,
        },
        ("keep", holdout_start, holdout_end, 5): {
            "sharpe": 0.8, "total_return": 0.12, "max_drawdown": -0.08, "n_trades": 12, "pending_exit_positions": 0,
        },
    }
    store = OptimizerExperimentStore(tmp_path)
    exp = run_search(
        _ScriptedService(table),
        store,
        experiment_id="so-0aaa00000004",
        config_hash="h4",
        objective="sharpe",
        scenarios=scenarios,
        requested_count=requested,
        truncated=truncated,
        train_start=train_start,
        train_end=train_end,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        min_trades=10,
        top_k=1,
        request_snapshot={"strategy_ids": ["keep"], "years": 8},
    )
    assert exp.status == "completed"
    assert exp.request == {"strategy_ids": ["keep"], "years": 8}
    loaded = store.load("so-0aaa00000004")
    assert loaded.request == {"strategy_ids": ["keep"], "years": 8}
