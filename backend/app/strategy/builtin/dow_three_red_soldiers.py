"""Dow three-red-soldiers continuation pattern."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_three_red_soldiers",
    "name": "三个红小兵",
    "description": "连续三根实体阳线且收盘依次抬高。",
    "tags": ["道式", "K线"],
    "strategy_role": "buy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "body_ratio_min",
            "label": "最小实体占比",
            "type": "float",
            "default": 0.5,
            "min": 0.35,
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
ENTRY_SIGNALS = ["dow_three_red_soldiers_entry"]
EXIT_SIGNALS = ["dow_three_red_soldiers_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("three_red_soldiers", "buy", 3, ENTRY_SIGNALS[0])
