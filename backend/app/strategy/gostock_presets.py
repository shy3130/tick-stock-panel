"""Executable local presets distilled from the GoStock screener examples."""
from __future__ import annotations

from typing import Any

GOSTOCK_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "strong_momentum",
        "name": "强势追涨",
        "description": "涨幅2-7% + 量比>2 + 换手>3% + 站上MA20",
        "predicate": {
            "conditions": [
                {"field": "change_pct", "op": "between", "value": [0.02, 0.07]},
                {"field": "vol_ratio_5d", "op": ">", "value": 2.0},
                {"field": "turnover_rate", "op": ">", "value": 3.0},
                {"field": "above_ma20", "op": "=", "value": True},
                {"field": "exclude_st", "op": "=", "value": True},
            ],
            "order_by": {"field": "change_pct", "direction": "desc"},
        },
        "executable_level": "full",
    },
    {
        "id": "bullish_macd",
        "name": "均线多头MACD金叉",
        "description": "均线多头排列 + MACD金叉 + 非ST",
        "predicate": {
            "conditions": [
                {"field": "ma_bullish_alignment", "op": "=", "value": True},
                {"field": "macd_golden", "op": "=", "value": True},
                {"field": "exclude_st", "op": "=", "value": True},
            ],
            "order_by": {"field": "vol_ratio_5d", "direction": "desc"},
        },
        "executable_level": "full",
    },
    {
        "id": "midcap_breakout",
        "name": "中盘突破",
        "description": "流通市值50-200亿 + 站上MA20 + 量比>1 + 换手>3%",
        "predicate": {
            "conditions": [
                {"field": "float_market_cap", "op": "between", "value": [50, 200]},
                {"field": "above_ma20", "op": "=", "value": True},
                {"field": "vol_ratio_5d", "op": ">", "value": 1.0},
                {"field": "turnover_rate", "op": ">", "value": 3.0},
                {"field": "exclude_st", "op": "=", "value": True},
            ],
            "order_by": {"field": "turnover_rate", "direction": "desc"},
        },
        "executable_level": "full",
    },
    {
        "id": "quality_growth",
        "name": "质优成长",
        "description": "净利润同比>50% + ROE>15% + 均线多头 (需基本面)",
        "predicate": {
            "conditions": [
                {"field": "yo_y_profit", "op": ">", "value": 50.0},
                {"field": "roe", "op": ">", "value": 15.0},
                {"field": "ma_bullish_alignment", "op": "=", "value": True},
            ],
            "order_by": {"field": "yo_y_profit", "direction": "desc"},
        },
        "executable_level": "needs_fundamental",
    },
    {
        "id": "consecutive_boards",
        "name": "连板强势",
        "description": "连板≥2 + 换手>5%",
        "predicate": {
            "conditions": [
                {"field": "consecutive_limit_ups", "op": ">=", "value": 2},
                {"field": "turnover_rate", "op": ">", "value": 5.0},
            ],
            "order_by": {"field": "consecutive_limit_ups", "direction": "desc"},
        },
        "executable_level": "full",
    },
)


def list_gostock_presets() -> list[dict[str, Any]]:
    return list(GOSTOCK_PRESETS)


__all__ = ["GOSTOCK_PRESETS", "list_gostock_presets"]
