"""Dow head-and-shoulders top risk warning."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_head_shoulders_top",
    "name": "头肩顶结构",
    "description": "跌破颈线，或跌破后反抽颈线失败。",
    "tags": ["道式", "形态", "风险"],
    "strategy_role": "risk",
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
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["dow_head_shoulders_top_risk"]
STOP_LOSS = None
MAX_HOLD_DAYS = None
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("head_shoulders_top", "risk", 20, EXIT_SIGNALS[0])
