"""Dow head-and-shoulders bottom confirmation."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_head_shoulders_bottom",
    "name": "头肩底结构",
    "description": "放量突破颈线，或突破后回踩颈线站稳。",
    "tags": ["道式", "形态"],
    "strategy_role": "buy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "neckline_tolerance_ratio",
            "label": "颈线容差",
            "type": "float",
            "default": 0.03,
            "min": 0.01,
            "max": 0.08,
            "step": 0.01,
        }
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
ENTRY_SIGNALS = ["dow_head_shoulders_bottom_entry"]
EXIT_SIGNALS = ["dow_head_shoulders_bottom_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("head_shoulders_bottom", "buy", 20, ENTRY_SIGNALS[0])
