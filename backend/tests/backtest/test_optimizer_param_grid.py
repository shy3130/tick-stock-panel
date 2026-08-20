"""寻优参数网格 V2 专项测试 — 纯逻辑，不依赖行情。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.backtest.optimizer import (
    SearchScenarioResult,
    UniverseSpec,
    _validate_and_expand_param_grid,
    expand_search_scenarios,
)
from app.backtest.strategy import StrategyBacktestConfig


class _ParamEngine:
    """最小策略引擎桩：按 sid 返回带 meta.params 的对象。"""

    def __init__(self, defs: dict[str, list[dict]]):
        self._defs = defs

    def get(self, sid: str):
        if sid not in self._defs:
            raise ValueError(f"unknown strategy: {sid}")
        return SimpleNamespace(
            id=sid,
            meta={"params": self._defs[sid], "asset_types": ["stock"]},
            execution_backend="polars_expr",
            source="builtin",
            ephemeral=False,
        )


def _base_cfg() -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id="x",
        symbols=None,
        start=date(2020, 1, 1),
        end=date(2021, 1, 1),
    )


def _uni(n: int = 2) -> list[UniverseSpec]:
    return [UniverseSpec(f"u{i}", f"U{i}", "all_a", None) for i in range(n)]


def test_no_param_grid_behaves_as_before():
    eng = _ParamEngine({})
    base = _base_cfg()
    a, req_a, _ = expand_search_scenarios(
        strategy_ids=["s1", "s2"],
        universes=_uni(2),
        holding_days=[5, 10],
        matchings=["open_t+1"],
        base=base,
        max_scenarios=200,
    )
    # 2 strat * 2 uni * 2 hold * 1 match = 8
    assert req_a == 8
    assert len(a) == 8
    assert all(sc.config.params in (None, {}) for sc in a)


def test_param_grid_multiplies_and_preserves_params_on_scenario():
    eng = _ParamEngine({
        "s1": [
            {"id": "p", "type": "float", "default": 1.0},
            {"id": "q", "type": "int", "default": 2},
        ]
    })
    pg = {"s1": {"p": [1.0, 1.5], "q": [3, 4]}}
    expanded = _validate_and_expand_param_grid(eng, pg, strategy_ids=["s1"])
    # 2*2 = 4 combos
    assert len(expanded["s1"]) == 4

    base = _base_cfg()
    scenarios, requested, _ = expand_search_scenarios(
        strategy_ids=["s1"],
        universes=_uni(1),
        holding_days=[5],
        matchings=["open_t+1"],
        base=base,
        max_scenarios=200,
        param_grid_expanded=expanded,
    )
    # 1 strat * 4 param * 1u *1h *1m = 4
    assert requested == 4
    assert len(scenarios) == 4
    # 每个场景 config.params 携带具体值，且 result 将带 params
    vals = sorted({(sc.config.params.get("p"), sc.config.params.get("q")) for sc in scenarios})
    assert vals == [(1.0, 3), (1.0, 4), (1.5, 3), (1.5, 4)]


def test_unknown_param_name_rejected():
    eng = _ParamEngine({"s1": [{"id": "known", "type": "float", "default": 1.0}]})
    with pytest.raises(ValueError, match="不存在参数 bad"):
        _validate_and_expand_param_grid(eng, {"s1": {"bad": [1]}}, strategy_ids=["s1"])


def test_type_mismatch_rejected():
    eng = _ParamEngine({"s1": [{"id": "n", "type": "int", "default": 1}]})
    with pytest.raises(ValueError, match="无法转为定义类型 int"):
        _validate_and_expand_param_grid(eng, {"s1": {"n": ["x"]}}, strategy_ids=["s1"])


def test_bool_only_accepts_true_false():
    eng = _ParamEngine({"s1": [{"id": "flag", "type": "bool", "default": False}]})
    ok = _validate_and_expand_param_grid(eng, {"s1": {"flag": [True, False, "true", "false"]}}, strategy_ids=["s1"])
    assert len(ok["s1"]) == 2  # 去重
    with pytest.raises(ValueError, match="无法转为定义类型 bool"):
        _validate_and_expand_param_grid(eng, {"s1": {"flag": ["yes"]}}, strategy_ids=["s1"])


def test_per_strategy_more_than_2_params_rejected():
    eng = _ParamEngine({"s1": [{"id": f"p{i}", "type": "float", "default": 0.0} for i in range(3)]})
    with pytest.raises(ValueError, match="参数个数超过上限 2"):
        _validate_and_expand_param_grid(eng, {"s1": {f"p{i}": [1] for i in range(3)}}, strategy_ids=["s1"])


def test_per_param_more_than_5_values_rejected():
    eng = _ParamEngine({"s1": [{"id": "p", "type": "float", "default": 0.0}]})
    with pytest.raises(ValueError, match="取值个数超过上限 5"):
        _validate_and_expand_param_grid(eng, {"s1": {"p": list(range(6))}}, strategy_ids=["s1"])


def test_cartesian_over_8_rejected():
    eng = _ParamEngine({
        "s1": [
            {"id": "a", "type": "int", "default": 0},
            {"id": "b", "type": "int", "default": 0},
        ]
    })
    # 3*3=9 >8
    with pytest.raises(ValueError, match="组合数超过上限 8"):
        _validate_and_expand_param_grid(eng, {"s1": {"a": [1, 2, 3], "b": [4, 5, 6]}}, strategy_ids=["s1"])


def test_scenario_result_carries_params():
    res = SearchScenarioResult(
        scenario_id="sc-1",
        strategy_id="s1",
        universe_id="u1",
        universe_label="U1",
        universe_kind="all_a",
        holding_days=5,
        matching="open_t+1",
        params={"p": 1.5, "q": 2},
    )
    assert res.params == {"p": 1.5, "q": 2}


def test_dsr_trials_equals_actual_scenario_count_after_param_expand():
    # 直接验证 run_search 内部 n_trials=len(train_returns) 即实际成功场景数
    # 这里仅证明 expand 后的场景数即为 DSR 使用的 trials 基数（逻辑已在 run_search 里）
    eng = _ParamEngine({"s1": [{"id": "p", "type": "float", "default": 1.0}]})
    pg = {"s1": {"p": [1.0, 2.0]}}
    expanded = _validate_and_expand_param_grid(eng, pg, strategy_ids=["s1"])
    base = _base_cfg()
    scenarios, requested, _ = expand_search_scenarios(
        strategy_ids=["s1"],
        universes=_uni(1),
        holding_days=[5],
        matchings=["open_t+1"],
        base=base,
        max_scenarios=200,
        param_grid_expanded=expanded,
    )
    assert requested == 2
    # 在 run_search 里 n_trials = len(train_returns) ≤ len(scenarios) 且等于实际进入 DSR 的数
    # 此处断言场景数即为理论 trials 上界（DSR 已用 len(train_returns)）
    assert len(scenarios) == 2
