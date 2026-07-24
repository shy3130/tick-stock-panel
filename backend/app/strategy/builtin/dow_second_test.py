"""Dow second-test buy point."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_second_test",
    "name": "二次测试",
    "description": "重要支撑再次测试后出现大阳线、长下影或反包。",
    "tags": ["道式", "买点"],
    "strategy_role": "buy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "tolerance_ratio",
            "label": "支撑容差",
            "type": "float",
            "default": 0.03,
            "min": 0.01,
            "max": 0.08,
            "step": 0.01,
        },
        {
            "id": "min_gap",
            "label": "最小间隔",
            "type": "int",
            "default": 3,
            "min": 2,
            "max": 10,
            "step": 1,
        },
        {
            "id": "max_gap",
            "label": "最大间隔",
            "type": "int",
            "default": 40,
            "min": 10,
            "max": 120,
            "step": 5,
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
ENTRY_SIGNALS = ["dow_second_test_entry"]
EXIT_SIGNALS = ["dow_second_test_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("second_test", "buy", 5, ENTRY_SIGNALS[0])
