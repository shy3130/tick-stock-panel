"""参数网格测试 — normalize / expand / hash / score / run_grid / 持久化 / panel 复用。

不依赖真实行情数据; 所有回测通过 mock service/stub 完成。
"""
from __future__ import annotations

import math
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.parameter_grid import (
    DEFAULT_MAX_SCENARIOS,
    HARD_MAX_SCENARIOS,
    GridExperiment,
    GridScenarioResult,
    NormalizedGrid,
    ParameterGridExperimentStore,
    compute_config_hash,
    compute_pareto_fronts,
    expand_scenarios,
    normalize_grid,
    run_grid,
    score_scenario,
)
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestResult, StrategyBacktestService
from app.strategy.engine import StrategyDef

# ── fixtures ──────────────────────────────────────

STRATEGY_PARAMS = [
    {"id": "vol_ratio_min", "label": "量比", "type": "float",
     "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    {"id": "top_n", "label": "TopN", "type": "int",
     "default": 10, "min": 1, "max": 50, "step": 1},
    {"id": "mode_select", "label": "Mode", "type": "select",
     "default": "a", "options": ["a", "b"]},
    {"id": "enabled", "label": "Enabled", "type": "bool", "default": True},
]

BASE_CFG = StrategyBacktestConfig(
    strategy_id="test_strat",
    symbols=["000001"],
    start=date(2024, 1, 1),
    end=date(2024, 6, 1),
)


def _base_config(**kw) -> StrategyBacktestConfig:
    base = dict(
        strategy_id="test_strat",
        symbols=["000001"],
        start=date(2024, 1, 1),
        end=date(2024, 6, 1),
    )
    base.update(kw)
    return StrategyBacktestConfig(**base)


# ================================================================
# normalize_grid
# ================================================================

class TestNormalizeGrid:

    def test_basic_float_param(self):
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0, 3.0]}, STRATEGY_PARAMS)
        assert ng.grid == {"vol_ratio_min": [1.0, 2.0, 3.0]}
        assert ng.requested_count == 3
        assert ng.scenario_count == 3
        assert not ng.truncated

    def test_int_param_coerced_to_int(self):
        ng = normalize_grid({"top_n": [5, 10, 20]}, STRATEGY_PARAMS)
        assert all(isinstance(v, int) for v in ng.grid["top_n"])
        assert "top_n" in ng.int_keys

    def test_int_param_rejects_non_integer(self):
        with pytest.raises(ValueError, match="不是整数"):
            normalize_grid({"top_n": [5.5]}, STRATEGY_PARAMS)

    def test_rejects_unknown_param(self):
        with pytest.raises(ValueError, match="不在策略数值参数白名单中"):
            normalize_grid({"unknown_param": [1.0]}, STRATEGY_PARAMS)

    def test_rejects_non_numeric_param(self):
        # select / bool 不在白名单
        with pytest.raises(ValueError, match="不在策略数值参数白名单中"):
            normalize_grid({"mode_select": ["a", "b"]}, STRATEGY_PARAMS)
        with pytest.raises(ValueError, match="不在策略数值参数白名单中"):
            normalize_grid({"enabled": [True, False]}, STRATEGY_PARAMS)

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError, match="超出范围"):
            normalize_grid({"vol_ratio_min": [0.1]}, STRATEGY_PARAMS)  # < 0.5
        with pytest.raises(ValueError, match="超出范围"):
            normalize_grid({"vol_ratio_min": [6.0]}, STRATEGY_PARAMS)  # > 5.0

    def test_rejects_nan(self):
        with pytest.raises(ValueError, match="非有限实数"):
            normalize_grid({"vol_ratio_min": [float("nan")]}, STRATEGY_PARAMS)

    def test_rejects_inf(self):
        with pytest.raises(ValueError, match="非有限实数"):
            normalize_grid({"vol_ratio_min": [float("inf")]}, STRATEGY_PARAMS)
        with pytest.raises(ValueError, match="非有限实数"):
            normalize_grid({"vol_ratio_min": [float("-inf")]}, STRATEGY_PARAMS)

    def test_rejects_bool_values(self):
        with pytest.raises(ValueError, match="非有限实数"):
            normalize_grid({"vol_ratio_min": [True, False]}, STRATEGY_PARAMS)

    def test_dedup_and_sort(self):
        ng = normalize_grid({"vol_ratio_min": [3.0, 1.0, 2.0, 1.0, 3.0]}, STRATEGY_PARAMS)
        assert ng.grid["vol_ratio_min"] == [1.0, 2.0, 3.0]

    def test_keys_sorted(self):
        ng = normalize_grid(
            {"top_n": [5, 10], "vol_ratio_min": [1.0, 2.0]},
            STRATEGY_PARAMS,
        )
        assert list(ng.grid.keys()) == ["top_n", "vol_ratio_min"]

    def test_truncation(self):
        # 2 × 3 × 5 = 30 > default 24 → truncated
        ng = normalize_grid(
            {
                "vol_ratio_min": [1.0, 2.0],
                "top_n": [5, 10, 15],
                # third axis not available — use vol_ratio_min only with big list
            },
            STRATEGY_PARAMS,
            max_scenarios=4,
        )
        assert ng.requested_count == 6
        assert ng.scenario_count == 4
        assert ng.truncated

    def test_hard_max_caps_at_36(self):
        # max_scenarios=100 → capped to 36
        ng = normalize_grid(
            {"vol_ratio_min": list(range(1, 6))},  # 5 values
            STRATEGY_PARAMS,
            max_scenarios=100,
        )
        assert ng.max_scenarios == HARD_MAX_SCENARIOS  # 36

    def test_empty_grid(self):
        ng = normalize_grid({}, STRATEGY_PARAMS)
        assert ng.grid == {}
        assert ng.requested_count == 1
        assert ng.scenario_count == 1
        assert not ng.truncated

    def test_default_max_is_24(self):
        ng = normalize_grid({"vol_ratio_min": [1.0]}, STRATEGY_PARAMS)
        assert ng.max_scenarios == DEFAULT_MAX_SCENARIOS


# ================================================================
# expand_scenarios
# ================================================================

class TestExpandScenarios:

    def test_cartesian_product(self):
        ng = normalize_grid(
            {"vol_ratio_min": [1.0, 2.0], "top_n": [5, 10]},
            STRATEGY_PARAMS,
        )
        scenarios = expand_scenarios(BASE_CFG, ng)
        assert len(scenarios) == 4
        # 验证所有组合都存在
        param_combos = sorted(
            (s.params["vol_ratio_min"], s.params["top_n"]) for s in scenarios
        )
        assert param_combos == [(1.0, 5), (1.0, 10), (2.0, 5), (2.0, 10)]

    def test_empty_grid_returns_base(self):
        ng = normalize_grid({}, STRATEGY_PARAMS)
        scenarios = expand_scenarios(BASE_CFG, ng)
        assert len(scenarios) == 1
        assert scenarios[0].strategy_id == BASE_CFG.strategy_id

    def test_int_params_preserved_as_int(self):
        ng = normalize_grid({"top_n": [5, 10]}, STRATEGY_PARAMS)
        scenarios = expand_scenarios(BASE_CFG, ng)
        for s in scenarios:
            assert isinstance(s.params["top_n"], int)

    def test_truncated_count(self):
        ng = normalize_grid(
            {"vol_ratio_min": [1.0, 2.0, 3.0, 4.0, 5.0]},
            STRATEGY_PARAMS,
            max_scenarios=3,
        )
        scenarios = expand_scenarios(BASE_CFG, ng)
        assert len(scenarios) == 3

    def test_non_grid_fields_unchanged(self):
        ng = normalize_grid({"vol_ratio_min": [1.0]}, STRATEGY_PARAMS)
        scenarios = expand_scenarios(BASE_CFG, ng)
        s = scenarios[0]
        assert s.strategy_id == BASE_CFG.strategy_id
        assert s.start == BASE_CFG.start
        assert s.end == BASE_CFG.end
        assert s.mode == BASE_CFG.mode

    def test_expansion_is_deterministic(self):
        ng = normalize_grid(
            {"vol_ratio_min": [2.0, 1.0], "top_n": [10, 5]},
            STRATEGY_PARAMS,
        )
        run1 = expand_scenarios(BASE_CFG, ng)
        run2 = expand_scenarios(BASE_CFG, ng)
        assert [s.params for s in run1] == [s.params for s in run2]


# ================================================================
# compute_config_hash
# ================================================================

class TestConfigHash:

    def test_same_input_same_hash(self):
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0]}, STRATEGY_PARAMS)
        h1 = compute_config_hash(BASE_CFG, ng.grid, "sharpe")
        h2 = compute_config_hash(BASE_CFG, ng.grid, "sharpe")
        assert h1 == h2

    def test_different_objective_different_hash(self):
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0]}, STRATEGY_PARAMS)
        h1 = compute_config_hash(BASE_CFG, ng.grid, "sharpe")
        h2 = compute_config_hash(BASE_CFG, ng.grid, "calmar")
        assert h1 != h2

    def test_different_grid_different_hash(self):
        ng1 = normalize_grid({"vol_ratio_min": [1.0, 2.0]}, STRATEGY_PARAMS)
        ng2 = normalize_grid({"vol_ratio_min": [1.0, 3.0]}, STRATEGY_PARAMS)
        h1 = compute_config_hash(BASE_CFG, ng1.grid, "sharpe")
        h2 = compute_config_hash(BASE_CFG, ng2.grid, "sharpe")
        assert h1 != h2

    def test_grid_key_order_irrelevant(self):
        ng_a = normalize_grid({"vol_ratio_min": [1.0], "top_n": [5]}, STRATEGY_PARAMS)
        # 手动构造逆序 grid
        reversed_grid = {"top_n": [5], "vol_ratio_min": [1.0]}
        h1 = compute_config_hash(BASE_CFG, ng_a.grid, "sharpe")
        h2 = compute_config_hash(BASE_CFG, reversed_grid, "sharpe")
        assert h1 == h2

    def test_different_symbols_different_hash(self):
        cfg2 = _base_config(symbols=["600000"])
        ng = normalize_grid({"vol_ratio_min": [1.0]}, STRATEGY_PARAMS)
        h1 = compute_config_hash(BASE_CFG, ng.grid, "sharpe")
        h2 = compute_config_hash(cfg2, ng.grid, "sharpe")
        assert h1 != h2


# ================================================================
# score_scenario
# ================================================================

class TestScoreScenario:

    def test_sharpe(self):
        stats = {"sharpe": 1.5, "total_return": 0.3, "max_drawdown": -0.1}
        assert score_scenario(stats, "sharpe") == 1.5

    def test_total_return(self):
        stats = {"sharpe": 1.5, "total_return": 0.3, "max_drawdown": -0.1}
        assert score_scenario(stats, "total_return") == 0.3

    def test_calmar(self):
        stats = {"sharpe": 1.5, "total_return": 0.3, "max_drawdown": -0.1}
        assert score_scenario(stats, "calmar") == round(0.3 / 0.1, 4)

    def test_calmar_zero_drawdown_positive_return(self):
        stats = {"sharpe": 1.0, "total_return": 0.2, "max_drawdown": 0.0}
        assert score_scenario(stats, "calmar") == round(0.2 * 100.0, 4)

    def test_calmar_zero_drawdown_zero_return(self):
        stats = {"sharpe": 0.0, "total_return": 0.0, "max_drawdown": 0.0}
        assert score_scenario(stats, "calmar") == 0.0

    def test_risk_adjusted(self):
        stats = {"sharpe": 1.5, "total_return": 0.3, "max_drawdown": -0.1}
        expected = round(1.5 + 0.3 / 0.1, 4)
        assert score_scenario(stats, "risk_adjusted") == expected

    def test_missing_stats_defaults_to_zero(self):
        assert score_scenario({}, "sharpe") == 0.0
        assert score_scenario({}, "total_return") == 0.0
        assert score_scenario({}, "calmar") == 0.0

    def test_deterministic(self):
        stats = {"sharpe": 2.0, "total_return": 0.5, "max_drawdown": -0.15}
        assert score_scenario(stats, "risk_adjusted") == score_scenario(stats, "risk_adjusted")


# ================================================================
# 严格 Pareto 分层
# ================================================================

class TestParetoFronts:

    def _scenario(
        self,
        scenario_id: str,
        total_return: float,
        sharpe: float,
        max_drawdown: float,
    ) -> GridScenarioResult:
        return GridScenarioResult(
            scenario_id=scenario_id,
            params={},
            stats={
                "total_return": total_return,
                "sharpe": sharpe,
                "max_drawdown": max_drawdown,
            },
        )

    def test_strict_non_dominated_layers(self):
        scenarios = [
            # front 1: 收益/夏普最高但回撤最大；front 1 也要保留风险更平衡的另一解
            self._scenario("s-high-return", 0.50, 2.0, -0.20),
            self._scenario("s-balanced", 0.40, 1.8, -0.10),
            # front 1 中的平衡解同时支配这两个场景
            self._scenario("s-worse", 0.39, 1.7, -0.10),
            self._scenario("s-worst", 0.39, 1.6, -0.12),
        ]

        fronts = compute_pareto_fronts(scenarios)

        assert fronts["s-high-return"] == 1
        assert fronts["s-balanced"] == 1
        assert fronts["s-worse"] == 2
        assert fronts["s-worst"] == 3

    def test_equal_objectives_do_not_dominate_each_other(self):
        scenarios = [
            self._scenario("s-a", 0.20, 1.0, -0.05),
            self._scenario("s-b", 0.20, 1.0, -0.05),
        ]

        fronts = compute_pareto_fronts(scenarios)

        assert fronts == {"s-a": 1, "s-b": 1}

    def test_invalid_or_errored_scenarios_are_excluded(self):
        scenarios = [
            self._scenario("s-good", 0.20, 1.0, -0.05),
            GridScenarioResult(scenario_id="s-error", params={}, error="boom"),
            GridScenarioResult(
                scenario_id="s-nan",
                params={},
                stats={
                    "total_return": float("nan"),
                    "sharpe": 99.0,
                    "max_drawdown": 0.0,
                },
            ),
        ]

        assert compute_pareto_fronts(scenarios) == {"s-good": 1}

    def test_run_grid_assigns_and_persists_fronts(self, tmp_path):
        stats_map = {
            frozenset({"vol_ratio_min": 1.0}.items()): {"sharpe": 1.0, "total_return": 0.10, "max_drawdown": -0.10},
            frozenset({"vol_ratio_min": 2.0}.items()): {"sharpe": 2.0, "total_return": 0.20, "max_drawdown": -0.10},
            frozenset({"vol_ratio_min": 3.0}.items()): {"sharpe": 0.5, "total_return": 0.05, "max_drawdown": -0.12},
        }
        svc = _ScriptedService(stats_map)
        store = ParameterGridExperimentStore(tmp_path)
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0, 3.0]}, STRATEGY_PARAMS, "sharpe")
        scenarios = expand_scenarios(BASE_CFG, ng)

        exp = run_grid(svc, store, BASE_CFG, scenarios, ng, "pg-9aef01000001", "h")
        by_param = {s.params["vol_ratio_min"]: s for s in exp.scenarios}

        # 1.0 被 2.0 严格支配，目标排序最好的 2.0 才在 Pareto 第一层。
        assert by_param[2.0].pareto_front == 1
        assert by_param[1.0].pareto_front == 2
        assert by_param[3.0].pareto_front == 3

        loaded = store.load("pg-9aef01000001")
        loaded_by_param = {s.params["vol_ratio_min"]: s for s in loaded.scenarios}
        assert loaded_by_param[2.0].pareto_front == 1
        assert loaded_by_param[1.0].pareto_front == 2
        assert loaded_by_param[3.0].pareto_front == 3


# ================================================================
# ParameterGridExperimentStore
# ================================================================

class TestExperimentStore:

    def test_save_load_roundtrip(self, tmp_path):
        store = ParameterGridExperimentStore(tmp_path)
        exp = GridExperiment(
            experiment_id="pg-deadbeef1234",
            config_hash="abc123",
            strategy_id="test",
            objective="sharpe",
            base_config={"strategy_id": "test"},
            grid={"x": [1.0]},
            requested_count=2,
            scenario_count=2,
            max_scenarios=24,
            truncated=False,
            status="completed",
            scenarios=[
                GridScenarioResult(scenario_id="s0000", params={"x": 1.0}, stats={"sharpe": 1.0}, score=1.0, rank=1),
            ],
            best_scenario_id="s0000",
            created_at="2024-01-01T00:00:00+00:00",
            updated_at="2024-01-01T00:00:00+00:00",
            completed=1,
            total=1,
        )
        store.save(exp)
        assert store.exists("pg-deadbeef1234")

        loaded = store.load("pg-deadbeef1234")
        assert loaded.experiment_id == "pg-deadbeef1234"
        assert loaded.config_hash == "abc123"
        assert len(loaded.scenarios) == 1
        assert loaded.scenarios[0].score == 1.0
        assert loaded.scenarios[0].rank == 1

    def test_load_nonexistent_raises_keyerror(self, tmp_path):
        store = ParameterGridExperimentStore(tmp_path)
        with pytest.raises(KeyError):
            store.load("pg-999999999999")

    def test_invalid_id_rejected(self, tmp_path):
        store = ParameterGridExperimentStore(tmp_path)
        with pytest.raises(ValueError):
            store.load("../etc/passwd")
        with pytest.raises(ValueError):
            store.load("not-a-valid-id")

    def test_no_tmp_file_left(self, tmp_path):
        store = ParameterGridExperimentStore(tmp_path)
        exp = GridExperiment(
            experiment_id="pg-aaaabbbbcccc",
            config_hash="h",
            strategy_id="t",
            objective="sharpe",
            base_config={},
            grid={},
            requested_count=1,
            scenario_count=1,
            max_scenarios=24,
            truncated=False,
        )
        store.save(exp)
        exp_dir = Path(tmp_path) / "research" / "parameter_grid_experiments"
        assert not list(exp_dir.glob("*.tmp"))

    def test_overwrite_via_save(self, tmp_path):
        store = ParameterGridExperimentStore(tmp_path)
        exp = GridExperiment(
            experiment_id="pg-ddddeeeeffff",
            config_hash="h",
            strategy_id="t",
            objective="sharpe",
            base_config={},
            grid={},
            requested_count=1,
            scenario_count=1,
            max_scenarios=24,
            truncated=False,
            status="running",
            completed=0,
            total=1,
        )
        store.save(exp)
        # 更新并重新保存
        exp.status = "completed"
        exp.completed = 1
        store.save(exp)
        loaded = store.load("pg-ddddeeeeffff")
        assert loaded.status == "completed"
        assert loaded.completed == 1

    def test_find_by_config_hash(self, tmp_path):
        store = ParameterGridExperimentStore(tmp_path)
        exp = GridExperiment(
            experiment_id="pg-111122223333",
            config_hash="hash_abc",
            strategy_id="t",
            objective="sharpe",
            base_config={},
            grid={},
            requested_count=1,
            scenario_count=1,
            max_scenarios=24,
            truncated=False,
            status="completed",
            updated_at="2024-06-01T00:00:00+00:00",
        )
        store.save(exp)
        found = store.find_by_config_hash("hash_abc")
        assert found is not None
        assert found.experiment_id == "pg-111122223333"
        assert store.find_by_config_hash("nope") is None


# ================================================================
# run_grid (mock service)
# ================================================================

class _MockStrategyEngine:
    """不实际加载策略; 测试用。"""

    def __init__(self, strategy: StrategyDef) -> None:
        self._strategy = strategy

    def get(self, strategy_id: str) -> StrategyDef:
        return self._strategy


class _MockEngine:
    """记录 load_panel 调用, 返回空 DataFrame。"""

    def __init__(self) -> None:
        self.load_panel_calls = 0

    def load_panel(self, symbols, start, end, columns=None):
        self.load_panel_calls += 1
        return pl.DataFrame()


def _make_strategy() -> StrategyDef:
    return StrategyDef(
        meta={"id": "test", "name": "test", "scoring": {}, "params": STRATEGY_PARAMS, "limit": 100},
        basic_filter={"enabled": False},
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


class _ScriptedService:
    """按 params 映射返回不同 stats 的 mock service, 验证排序。

    stats_map: frozenset(params.items()) -> stats dict
    """

    def __init__(self, stats_map: dict) -> None:
        self._stats_map = stats_map
        self.engine = _MockEngine()
        self.run_count = 0
        self.panels_passed: list = []

    def compute_load_range(self, config):
        return config.start, config.end

    def run(self, config, progress_cb=None, cancel_event=None, panel=None):
        self.run_count += 1
        if panel is not None:
            self.panels_passed.append(panel)
        if cancel_event is not None and cancel_event.is_set():
            return StrategyBacktestResult(
                run_id="cancel", config={}, error="cancelled", elapsed_ms=1.0,
            )
        key = frozenset((config.params or {}).items())
        stats = self._stats_map.get(key, {"sharpe": 0.0, "total_return": 0.0, "max_drawdown": 0.0})
        return StrategyBacktestResult(
            run_id=f"r{self.run_count}",
            config={},
            stats=stats,
            equity_curve=[
                {"date": "2024-01-01", "value": 1000000.0},
                {"date": "2024-01-02", "value": 1050000.0},
                {"date": "2024-01-03", "value": 1100000.0},
            ],
            trades=[],
            elapsed_ms=10.0,
        )


class TestRunGrid:

    def test_scoring_and_ranking(self, tmp_path):
        stats_map = {
            frozenset({"vol_ratio_min": 1.0}.items()): {"sharpe": 1.0, "total_return": 0.1, "max_drawdown": -0.05},
            frozenset({"vol_ratio_min": 2.0}.items()): {"sharpe": 2.0, "total_return": 0.2, "max_drawdown": -0.05},
            frozenset({"vol_ratio_min": 3.0}.items()): {"sharpe": 0.5, "total_return": 0.05, "max_drawdown": -0.05},
        }
        svc = _ScriptedService(stats_map)
        store = ParameterGridExperimentStore(tmp_path)

        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0, 3.0]}, STRATEGY_PARAMS, "sharpe")
        scenarios = expand_scenarios(BASE_CFG, ng)

        exp = run_grid(svc, store, BASE_CFG, scenarios, ng, "pg-deadbeef0001", "hash1")

        assert exp.status == "completed"
        assert len(exp.scenarios) == 3
        # rank 1 = highest sharpe = vol_ratio_min 2.0
        best = next(s for s in exp.scenarios if s.scenario_id == exp.best_scenario_id)
        assert best.params["vol_ratio_min"] == 2.0
        assert best.rank == 1
        assert best.score == 2.0
        # ranks assigned
        ranked = sorted(exp.scenarios, key=lambda s: s.rank)
        assert [s.params["vol_ratio_min"] for s in ranked] == [2.0, 1.0, 3.0]

    def test_truncated_flag_preserved(self, tmp_path):
        svc = _ScriptedService({})
        store = ParameterGridExperimentStore(tmp_path)
        ng = normalize_grid(
            {"vol_ratio_min": [1.0, 2.0, 3.0, 4.0, 5.0]},
            STRATEGY_PARAMS, max_scenarios=3,
        )
        scenarios = expand_scenarios(BASE_CFG, ng)
        exp = run_grid(svc, store, BASE_CFG, scenarios, ng, "pg-cafe00000001", "h")

        assert exp.truncated is True
        assert exp.scenario_count == 3
        assert len(exp.scenarios) == 3

    def test_experiment_persisted_and_reloadable(self, tmp_path):
        svc = _ScriptedService({})
        store = ParameterGridExperimentStore(tmp_path)
        ng = normalize_grid({"vol_ratio_min": [1.0]}, STRATEGY_PARAMS)
        scenarios = expand_scenarios(BASE_CFG, ng)
        exp = run_grid(svc, store, BASE_CFG, scenarios, ng, "pg-bead00000001", "h")

        # 从磁盘重新加载
        loaded = store.load("pg-bead00000001")
        assert loaded.experiment_id == "pg-bead00000001"
        assert loaded.status == "completed"
        assert len(loaded.scenarios) == 1

    def test_cancel_interrupts(self, tmp_path):
        svc = _ScriptedService({})
        store = ParameterGridExperimentStore(tmp_path)
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0, 3.0]}, STRATEGY_PARAMS)
        scenarios = expand_scenarios(BASE_CFG, ng)
        cancel_event = threading.Event()
        cancel_event.set()  # 预设取消

        exp = run_grid(
            svc, store, BASE_CFG, scenarios, ng,
            "pg-face00000001", "h", cancel_event=cancel_event,
        )
        assert exp.status == "cancelled"
        # 所有 scenario 都返回 cancelled error
        assert all(s.error == "cancelled" for s in exp.scenarios)
        # 没有有效 rank
        assert all(s.rank == 0 for s in exp.scenarios)

    def test_best_scenario_robustness(self, tmp_path):
        stats_map = {
            frozenset({"vol_ratio_min": 1.0}.items()): {"sharpe": 0.5, "total_return": 0.05, "max_drawdown": -0.02},
            frozenset({"vol_ratio_min": 2.0}.items()): {"sharpe": 3.0, "total_return": 0.4, "max_drawdown": -0.08},
        }
        svc = _ScriptedService(stats_map)
        store = ParameterGridExperimentStore(tmp_path)
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0]}, STRATEGY_PARAMS, "sharpe")
        scenarios = expand_scenarios(BASE_CFG, ng)

        exp = run_grid(svc, store, BASE_CFG, scenarios, ng, "pg-feed00000001", "h")

        assert exp.robustness is not None
        assert "bootstrap" in exp.robustness
        assert "ci_low" in exp.robustness["bootstrap"]
        assert "ci_high" in exp.robustness["bootstrap"]
        assert "mc_permutation" in exp.robustness
        assert 0.0 <= exp.robustness["mc_permutation"]["p_value"] <= 1.0

    def test_candidate_best_scenario_skips_daily_frequency_metrics(self, tmp_path):
        """退出事件日采样曲线不是日频收益，候选网格不得生成 Bootstrap/置换 Sharpe。"""
        stats_map = {
            frozenset({"vol_ratio_min": 1.0}.items()): {
                "mode": "full",
                "full_kind": "candidate_execution",
                "sharpe": None,
                "total_return": 0.05,
                "max_drawdown": -0.02,
            },
            frozenset({"vol_ratio_min": 2.0}.items()): {
                "mode": "full",
                "full_kind": "candidate_execution",
                "sharpe": None,
                "total_return": 0.4,
                "max_drawdown": -0.08,
            },
        }
        svc = _ScriptedService(stats_map)
        store = ParameterGridExperimentStore(tmp_path)
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0]}, STRATEGY_PARAMS, "total_return")
        scenarios = expand_scenarios(BASE_CFG, ng)

        exp = run_grid(svc, store, BASE_CFG, scenarios, ng, "pg-feed00000002", "h")

        assert exp.robustness is not None
        assert exp.robustness["time_series_metrics_unavailable"] == "candidate_execution"
        assert "bootstrap" not in exp.robustness
        assert "mc_permutation" not in exp.robustness
        assert exp.robustness["exit_breakdown"] == []

        loaded = store.load("pg-feed00000002")
        assert loaded.robustness["time_series_metrics_unavailable"] == "candidate_execution"
        assert "bootstrap" not in loaded.robustness
        assert "mc_permutation" not in loaded.robustness

    def test_robustness_deterministic_across_runs(self, tmp_path):
        stats_map = {
            frozenset({"vol_ratio_min": 1.0}.items()): {"sharpe": 2.0, "total_return": 0.3, "max_drawdown": -0.1},
            frozenset({"vol_ratio_min": 2.0}.items()): {"sharpe": 1.0, "total_return": 0.1, "max_drawdown": -0.05},
        }

        def _run_once(eid):
            svc = _ScriptedService(stats_map)
            store = ParameterGridExperimentStore(tmp_path)
            ng = normalize_grid({"vol_ratio_min": [1.0, 2.0]}, STRATEGY_PARAMS, "sharpe")
            scenarios = expand_scenarios(BASE_CFG, ng)
            return run_grid(svc, store, BASE_CFG, scenarios, ng, eid, "h")

        exp1 = _run_once("pg-decaf00000001")
        exp2 = _run_once("pg-decaf00000002")

        # 最优 scenario 相同
        assert exp1.best_scenario_id == exp2.best_scenario_id
        # 排序相同
        r1 = [s.scenario_id for s in sorted(exp1.scenarios, key=lambda x: x.rank) if s.rank > 0]
        r2 = [s.scenario_id for s in sorted(exp2.scenarios, key=lambda x: x.rank) if s.rank > 0]
        assert r1 == r2
        # bootstrap CI 相同 (seed=42 固定)
        assert exp1.robustness["bootstrap"]["ci_low"] == exp2.robustness["bootstrap"]["ci_low"]
        assert exp1.robustness["mc_permutation"]["p_value"] == exp2.robustness["mc_permutation"]["p_value"]

    def test_progress_cb_invoked(self, tmp_path):
        svc = _ScriptedService({})
        store = ParameterGridExperimentStore(tmp_path)
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0]}, STRATEGY_PARAMS)
        scenarios = expand_scenarios(BASE_CFG, ng)
        events = []
        run_grid(
            svc, store, BASE_CFG, scenarios, ng,
            "pg-abcd00000001", "h",
            progress_cb=lambda evt: events.append(evt),
        )
        assert len(events) == 2
        assert all(e["type"] == "scenario_done" for e in events)
        assert events[-1]["completed"] == 2

    def test_shared_panel_passed_to_all_scenarios(self, tmp_path):
        """验证 panel 复用: 所有 scenario 收到同一 panel 引用。"""
        svc = _ScriptedService({})
        store = ParameterGridExperimentStore(tmp_path)
        ng = normalize_grid({"vol_ratio_min": [1.0, 2.0, 3.0]}, STRATEGY_PARAMS)
        scenarios = expand_scenarios(BASE_CFG, ng)
        run_grid(svc, store, BASE_CFG, scenarios, ng, "pg-ace000000001", "h")

        # 所有 scenario 收到同一 panel (空 DataFrame, 同一对象)
        assert len(svc.panels_passed) == 3
        assert all(p is svc.panels_passed[0] for p in svc.panels_passed)
        # engine.load_panel 只调用一次 (预加载)
        assert svc.engine.load_panel_calls == 1


# ================================================================
# panel 复用: StrategyBacktestService.run(panel=...)
# ================================================================

class TestPanelReuse:

    def test_run_with_panel_skips_load(self):
        """传入 panel 时不再调用 engine.load_panel。"""
        panel = pl.DataFrame({
            "symbol": ["A"], "name": ["A"], "date": [date(2024, 1, 1)],
            "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
            "volume": [100], "amount": [1000.0],
            "signal_limit_up": [False], "signal_limit_down": [False],
        }).sort(["symbol", "date"])

        engine = SimpleNamespace(
            repo=SimpleNamespace(get_index_daily=lambda *a, **k: pl.DataFrame()),
            load_panel=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not load")),
        )

        class _EngineWrap:
            repo = engine.repo
            def load_panel(self, *a, **k):
                raise AssertionError("should not call load_panel when panel provided")
            def simulate_portfolio(self, *a, **k):
                from app.backtest.engine import SimResult
                return SimResult(equity_curve=[], drawdown_curve=[], trades=[], per_symbol_stats=[], stats={"total_return": 0.0, "n_trades": 0})

        svc = StrategyBacktestService(_EngineWrap(), _MockStrategyEngine(_make_strategy()))
        result = svc.run(StrategyBacktestConfig(
            strategy_id="test", symbols=None,
            start=date(2024, 1, 1), end=date(2024, 1, 1),
            matching="close_t", mode="position",
        ), panel=panel)
        assert result.error is None

    def test_run_without_panel_loads_normally(self):
        """不传 panel 时走正常 load_panel 路径 (旧路径不变)。"""
        panel = pl.DataFrame({
            "symbol": ["A"], "name": ["A"], "date": [date(2024, 1, 1)],
            "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
            "volume": [100], "amount": [1000.0],
            "signal_limit_up": [False], "signal_limit_down": [False],
        }).sort(["symbol", "date"])

        load_calls = [0]

        class _EngineWrap:
            repo = SimpleNamespace(get_index_daily=lambda *a, **k: pl.DataFrame())
            def load_panel(self, *a, **k):
                load_calls[0] += 1
                return panel
            def simulate_portfolio(self, *a, **k):
                from app.backtest.engine import SimResult
                return SimResult(equity_curve=[], drawdown_curve=[], trades=[], per_symbol_stats=[], stats={"total_return": 0.0, "n_trades": 0})

        svc = StrategyBacktestService(_EngineWrap(), _MockStrategyEngine(_make_strategy()))
        svc.run(StrategyBacktestConfig(
            strategy_id="test", symbols=None,
            start=date(2024, 1, 1), end=date(2024, 1, 1),
            matching="close_t", mode="position",
        ))
        assert load_calls[0] == 1

    def test_compute_load_range(self):
        """compute_load_range 返回合理的 load 区间 (含 warmup)。"""
        from app.backtest.engine import SimResult

        class _EngineWrap:
            repo = SimpleNamespace(get_index_daily=lambda *a, **k: pl.DataFrame())
            def load_panel(self, *a, **k): return pl.DataFrame()
            def simulate_portfolio(self, *a, **k):
                return SimResult(equity_curve=[], drawdown_curve=[], trades=[], per_symbol_stats=[], stats={})

        svc = StrategyBacktestService(_EngineWrap(), _MockStrategyEngine(_make_strategy()))
        cfg = StrategyBacktestConfig(
            strategy_id="test", symbols=None,
            start=date(2024, 1, 1), end=date(2024, 6, 1),
            matching="close_t", mode="position",
        )
        load_start, load_end = svc.compute_load_range(cfg)
        assert load_start < cfg.start  # warmup 往前推
        assert load_end == cfg.end  # position 模式


# ================================================================
# 旧单次回测不变: strategy.py run() 向后兼容
# ================================================================

class TestBackwardCompatibility:

    def test_run_signature_accepts_old_positional_args(self):
        """run(progress_cb, cancel_event) 仍可用位置参数调用 (panel 是 kw-only)。"""
        # 这个测试验证 panel 是 keyword-only, 不破坏旧的位置参数调用
        import inspect
        sig = inspect.signature(StrategyBacktestService.run)
        params = list(sig.parameters.values())
        # 通过类访问时首个参数是 self; config/progress_cb/cancel_event 为位置或关键字, panel 为 keyword-only
        assert params[0].name == "self"
        assert params[1].name == "config"
        assert params[4].name == "panel"
        assert params[4].kind == inspect.Parameter.KEYWORD_ONLY
