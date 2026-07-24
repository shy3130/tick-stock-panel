"""Dow morning-star reversal."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_morning_star",
    "name": "启明星",
    "description": "三根K线完成底部反转确认。",
    "tags": ["道式", "K线"],
    "strategy_role": "buy",
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
ENTRY_SIGNALS = ["dow_morning_star_entry"]
EXIT_SIGNALS = ["dow_morning_star_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("morning_star", "buy", 3, ENTRY_SIGNALS[0])
