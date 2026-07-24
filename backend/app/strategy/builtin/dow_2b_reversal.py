"""Dow 2B five-wave reversal."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_2b_reversal",
    "name": "2B结构",
    "description": "完整五浪下跌后破低，下一根K线立即以大阳线收回。",
    "tags": ["道式", "买点"],
    "strategy_role": "buy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "max_wave_span",
            "label": "最大浪形跨度",
            "type": "int",
            "default": 120,
            "min": 40,
            "max": 260,
            "step": 10,
        },
        {
            "id": "body_ratio_min",
            "label": "最小实体占比",
            "type": "float",
            "default": 0.55,
            "min": 0.4,
            "max": 0.8,
            "step": 0.05,
        },
    ],
    "basic_filter": {
        "price_min": 0,
        "price_max": 1_000_000,
        "market_cap_min": 0,
        "amount_min": 0,
        "exclude_st": False,
        "exclude_new_days": 0,
    },
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}
EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["dow_2b_reversal_entry"]
EXIT_SIGNALS = ["dow_2b_reversal_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("two_b_reversal", "buy", 7, ENTRY_SIGNALS[0])
