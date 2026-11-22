"""信号回测统计里的 inf/NaN 必须在出接口前清成 null。

Starlette 的 JSONResponse 用 json.dumps(..., allow_nan=False) 渲染, 响应体里
只要出现一个 inf/NaN, 整个 POST /api/backtest/run 直接 500
(ValueError: Out of range float values are not JSON compliant)。

统计值可能来自原生 float 或 NumPy 标量，两种非有限值都要清洗。
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from app.services.backtest import _config_to_dict, _json_safe


def _render_like_starlette(payload) -> str:
    """与 starlette.responses.JSONResponse.render 同参数。"""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@pytest.mark.parametrize(
    "value",
    [
        float("inf"),          # Profit Factor: 没有亏损单
        float("-inf"),
        float("nan"),          # Sharpe / Sortino: 零波动
        np.float64("inf"),
        np.float64("nan"),
    ],
)
def test_non_finite_values_become_null(value):
    assert _json_safe(value) is None


def test_stats_dict_survives_starlette_json_render():
    """统计字典清洗后可被 allow_nan=False 渲染。"""
    stats_dict = {
        "Total Return [%]": 12.5,
        "Profit Factor": float("inf"),   # 全胜 → gross_loss = 0
        "Sharpe Ratio": float("nan"),
        "Max Drawdown [%]": np.float64(3.25),
        "Start": date(2026, 1, 2),
    }

    cleaned = {k: _json_safe(v) for k, v in stats_dict.items()}
    rendered = json.loads(_render_like_starlette(cleaned))

    assert rendered["Profit Factor"] is None
    assert rendered["Sharpe Ratio"] is None
    assert rendered["Total Return [%]"] == 12.5
    assert rendered["Max Drawdown [%]"] == 3.25
    assert rendered["Start"] == "2026-01-02"


def test_finite_and_non_float_values_pass_through():
    """回归保护: 有限数值/字符串/bool/None/日期的既有行为不变。"""
    assert _json_safe(1.5) == 1.5
    assert _json_safe(np.float64(2.5)) == 2.5
    assert _json_safe(3) == 3
    assert _json_safe(np.int64(4)) == 4
    assert _json_safe("abc") == "abc"
    assert _json_safe(True) is True
    assert _json_safe(None) is None
    assert _json_safe(date(2026, 1, 2)) == "2026-01-02"


def test_config_payload_is_json_renderable():
    """/api/backtest/run 的 config 段本身无非法浮点 (对照组)。"""
    from app.services.backtest import BacktestConfig

    cfg = BacktestConfig(
        symbols=["600000.SH"],
        start=date(2026, 1, 2),
        end=date(2026, 2, 2),
        entries=["signal_ma_golden_5_20"],
        exits=[],
    )
    _render_like_starlette(_config_to_dict(cfg))
