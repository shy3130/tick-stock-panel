"""Dow evening-star risk warning."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_evening_star",
    "name": "黄昏之星",
    "description": "三根K线完成顶部反转确认。",
    "tags": ["道式", "K线", "风险"],
    "strategy_role": "risk",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "body_ratio_min",
            "label": "最小实体占比",
            "type": "float",
            "default": 0.55,
            "min": 0.4,
            "max": 0.8,
            "step": 0.05,
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
EXIT_SIGNALS = ["dow_evening_star_risk"]
STOP_LOSS = None
MAX_HOLD_DAYS = None
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("evening_star", "risk", 3, EXIT_SIGNALS[0])
