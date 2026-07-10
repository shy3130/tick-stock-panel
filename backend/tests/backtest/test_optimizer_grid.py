"""参数网格展开与校验测试 — 优化器的纯逻辑核心。

被测:
- expand_param_grid(params_meta, param_grid): 校验 + 笛卡尔积 -> 参数组合列表
- count_combinations(params_meta, param_grid): 组合数 (不真正展开, 用于爆炸预判)
"""
from __future__ import annotations

import pytest

from app.backtest.optimizer import (
    GRID_MAX_COMBINATIONS,
    count_combinations,
    expand_param_grid,
)

# 模拟一个策略的 params meta (对齐 StrategyDef.meta["params"] 结构)
PARAMS_META = [
    {"id": "ma_proximity", "type": "float", "default": 0.02, "min": 0.01, "max": 0.05, "step": 0.005},
    {"id": "min_boards", "type": "int", "default": 2, "min": 1, "max": 20, "step": 1},
    {"id": "use_ma20", "type": "bool", "default": True},
    {"id": "fill", "type": "select", "default": "close_t", "options": ["close_t", "open_t+1"]},
]


# ---------------------------------------------------------------
# 显式候选值列表
# ---------------------------------------------------------------

def test_explicit_value_lists_cartesian_product():
    grid = {"ma_proximity": [0.01, 0.02], "min_boards": [2, 3, 4]}
    combos = expand_param_grid(PARAMS_META, grid)
    assert len(combos) == 6  # 2 x 3
    assert {"ma_proximity": 0.01, "min_boards": 2} in combos
    assert {"ma_proximity": 0.02, "min_boards": 4} in combos


def test_single_param_sweep():
    combos = expand_param_grid(PARAMS_META, {"min_boards": [1, 5, 10]})
    assert combos == [{"min_boards": 1}, {"min_boards": 5}, {"min_boards": 10}]


def test_bool_and_select_sweep():
    grid = {"use_ma20": [True, False], "fill": ["close_t", "open_t+1"]}
    combos = expand_param_grid(PARAMS_META, grid)
    assert len(combos) == 4


# ---------------------------------------------------------------
# 范围 spec {min,max,step} 自动展开
# ---------------------------------------------------------------

def test_range_spec_expands_by_step():
    combos = expand_param_grid(PARAMS_META, {"ma_proximity": {"min": 0.01, "max": 0.03, "step": 0.01}})
    vals = sorted(c["ma_proximity"] for c in combos)
    assert vals == [0.01, 0.02, 0.03]  # 含端点


def test_range_spec_float_keeps_endpoint_despite_accumulation():
    """0.1 步长的浮点累加易丢端点 (0.1+0.1+0.1=0.30000004); 整数计数必须保住 0.3。"""
    meta = [{"id": "p", "type": "float", "default": 0.2, "min": 0.1, "max": 0.3, "step": 0.1}]
    combos = expand_param_grid(meta, {"p": {"min": 0.1, "max": 0.3, "step": 0.1}})
    vals = sorted(c["p"] for c in combos)
    assert vals == [0.1, 0.2, 0.3]  # 含端点 0.3, 不丢


def test_duplicate_values_folded():
    combos = expand_param_grid(PARAMS_META, {"ma_proximity": [0.02, 0.02, 0.03]})
    vals = sorted(c["ma_proximity"] for c in combos)
    assert vals == [0.02, 0.03]  # 去重


def test_range_spec_int_yields_ints():
    combos = expand_param_grid(PARAMS_META, {"min_boards": {"min": 1, "max": 4, "step": 1}})
    vals = sorted(c["min_boards"] for c in combos)
    assert vals == [1, 2, 3, 4]
    assert all(isinstance(v, int) for v in vals)


# ---------------------------------------------------------------
# 校验: 拒绝非法 grid
# ---------------------------------------------------------------

def test_unknown_param_rejected():
    with pytest.raises(ValueError, match="不存在"):
        expand_param_grid(PARAMS_META, {"nonexistent": [1, 2]})


def test_value_out_of_range_rejected():
    with pytest.raises(ValueError, match=r"超出范围|范围"):
        expand_param_grid(PARAMS_META, {"ma_proximity": [0.01, 0.99]})


def test_select_value_not_in_options_rejected():
    with pytest.raises(ValueError, match=r"options|选项"):
        expand_param_grid(PARAMS_META, {"fill": ["close_t", "bad_value"]})


def test_empty_grid_rejected():
    with pytest.raises(ValueError, match=r"空|至少"):
        expand_param_grid(PARAMS_META, {})


def test_combination_explosion_rejected():
    # 构造超过硬上限的组合
    big = {"ma_proximity": {"min": 0.01, "max": 0.05, "step": 0.001}}  # 41 个
    # 单参数 41 个不会爆; 用多参数放大
    grid = {
        "ma_proximity": {"min": 0.01, "max": 0.05, "step": 0.001},   # 41
        "min_boards": {"min": 1, "max": 20, "step": 1},              # 20
    }  # 41 x 20 = 820, 仍 < 2000; 再加一维
    grid["use_ma20"] = [True, False]  # x2 = 1640
    # 到这仍 < 2000, 断言 count 正确
    assert count_combinations(PARAMS_META, grid) == 1640
    assert count_combinations(PARAMS_META, big) == 41
    # 显式超限
    huge = {
        "ma_proximity": {"min": 0.01, "max": 0.05, "step": 0.001},   # 41
        "min_boards": {"min": 1, "max": 20, "step": 1},              # 20
        "fill": ["close_t", "open_t+1"],                             # 2
        "use_ma20": [True, False],                                   # 2
    }  # 41x20x2x2 = 3280 > 2000
    assert count_combinations(PARAMS_META, huge) > GRID_MAX_COMBINATIONS
    with pytest.raises(ValueError, match=r"组合数|上限|超过"):
        expand_param_grid(PARAMS_META, huge)
