"""策略 params 归一化测试 (issue #68 回归).

回归点: custom/AI 策略的 META["params"] 若是 dict / list[str] 等非标准格式,
原实现会在 _strategy_detail() 的 {p["id"]: p["default"] for p in params} 处抛
TypeError, 导致整个 /api/strategies 列表 500. 加载边界归一化后保证 params 永远
是标准 list[dict], 下游推导式天然安全.
"""
from __future__ import annotations

from app.api.strategy import _strategy_detail
from app.strategy.engine import _normalize_param_defs, StrategyDef


# ── 归一化各格式分支 ──────────────────────────────────────────────────


def test_none_returns_empty_list():
    assert _normalize_param_defs(None) == []


def test_standard_list_dict_keeps_and_fills_defaults():
    """标准 list[dict]: 保持结构, 补齐缺失的 label/type/default."""
    params = [
        {"id": "lookback", "label": "回看天数", "type": "int", "default": 7,
         "min": 3, "max": 30, "step": 1},
        {"id": "threshold", "type": "float", "default": 0.1},  # 缺 label
        {"id": "flag", "default": True},  # 缺 label/type
    ]
    result = _normalize_param_defs(params)
    assert len(result) == 3
    # 完整项原样保留
    assert result[0]["id"] == "lookback"
    assert result[0]["min"] == 3
    # 缺字段被补齐
    assert result[1]["label"] == "threshold"
    assert result[2]["label"] == "flag"
    assert result[2]["type"] == "float"  # 默认 type


def test_dict_simple_values():
    """dict 格式 - 纯值: {"lookback": 20} → [{id, default}]."""
    params = {"lookback": 20, "threshold": 0.15}
    result = _normalize_param_defs(params)
    by_id = {p["id"]: p for p in result}
    assert by_id["lookback"]["default"] == 20
    assert by_id["threshold"]["default"] == 0.15
    # 补齐默认字段
    assert by_id["lookback"]["label"] == "lookback"
    assert by_id["lookback"]["type"] == "float"


def test_dict_nested_definitions():
    """dict 格式 - 嵌套定义: {"k": {"default": 1, "type": "int"}} → 合并."""
    params = {"lookback": {"default": 20, "type": "int", "min": 3}}
    result = _normalize_param_defs(params)
    assert len(result) == 1
    assert result[0]["id"] == "lookback"
    assert result[0]["default"] == 20
    assert result[0]["type"] == "int"
    assert result[0]["min"] == 3


def test_list_of_strings():
    """list[str]: ["k1", "k2"] → [{id: "k1"}, {id: "k2"}], default=None."""
    params = ["lookback", "threshold"]
    result = _normalize_param_defs(params)
    assert len(result) == 2
    assert result[0]["id"] == "lookback"
    assert result[0]["default"] is None
    assert result[1]["id"] == "threshold"


def test_invalid_type_degrades_to_empty():
    """整体类型不可识别 (int/str/bool) → 降级为空 list, 不抛异常."""
    assert _normalize_param_defs(42) == []
    assert _normalize_param_defs("lookback") == []
    assert _normalize_param_defs(True) == []


def test_mixed_dirty_items_drop_unrecognized():
    """混合脏项: dict 项保留, 不可识别项 (int/None) 丢弃."""
    params = [
        {"id": "valid", "default": 10},
        42,        # 丢弃
        None,      # 丢弃
        "str_id",  # 保留作 id
    ]
    result = _normalize_param_defs(params)
    ids = [p["id"] for p in result]
    assert ids == ["valid", "str_id"]


def test_dict_item_missing_id_dropped():
    """list[dict] 里某项缺 id → 该项丢弃, 其他不受影响."""
    params = [
        {"id": "ok", "default": 1},
        {"label": "no id here"},  # 无 id, 丢弃
        {"id": "ok2"},
    ]
    result = _normalize_param_defs(params)
    ids = [p["id"] for p in result]
    assert ids == ["ok", "ok2"]


# ── issue #68 核心: _strategy_detail 不再 500 ──────────────────────


def _make_strategy_with_params(params) -> StrategyDef:
    """构造 META["params"] = params 的策略 (模拟非标准格式的 custom/AI 文件)."""
    return StrategyDef(
        meta={"id": "test_strat", "name": "测试", "params": params},
        basic_filter={"enabled": True},
        entry_signals=[], exit_signals=[],
        stop_loss=None, trailing_stop=None,
        trailing_take_profit_activate=None, trailing_take_profit_drawdown=None,
        max_hold_days=None, alerts=[],
        filter_fn=None, filter_history_fn=None,
        lookback_days=60, source="custom",
    )


def test_strategy_detail_survives_dict_params():
    """issue #68 核心: params 是 dict 时 _strategy_detail 不再抛 TypeError."""
    # 注: 实际加载会经 _load_file 归一化; 这里模拟"已归一化后"的状态,
    # 直接证明归一化产物能让 _strategy_detail 安全运行.
    raw_params = {"lookback": 20, "threshold": 0.15}
    normalized = _normalize_param_defs(raw_params)
    s = _make_strategy_with_params(normalized)
    detail = _strategy_detail(s)  # 不应抛异常
    assert detail["params_defaults"] == {"lookback": 20, "threshold": 0.15}


def test_strategy_detail_survives_list_str_params():
    """issue #68: params 是 list[str] 时也不再 500."""
    normalized = _normalize_param_defs(["lookback", "threshold"])
    s = _make_strategy_with_params(normalized)
    detail = _strategy_detail(s)
    assert detail["params_defaults"] == {"lookback": None, "threshold": None}


def test_strategy_detail_survives_empty_params():
    """整体非法格式降级为空 list 时, _strategy_detail 正常返回空 params_defaults."""
    normalized = _normalize_param_defs(42)  # 降级为 []
    s = _make_strategy_with_params(normalized)
    detail = _strategy_detail(s)
    assert detail["params_defaults"] == {}
    assert detail["params"] == []
