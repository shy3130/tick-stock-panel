"""Old-note bottom pregnancy pattern."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_bottom_pregnancy",
    "name": "底部孕线",
    "description": "小实体K线后出现放量大阳线，且高低点完全吞没前一根。",
    "tags": ["道式", "K线"],
    "strategy_role": "buy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "small_body_max",
            "label": "小实体最大占比",
            "type": "float",
            "default": 0.25,
            "min": 0.05,
            "max": 0.4,
            "step": 0.05,
        },
        {
            "id": "large_body_min",
            "label": "大实体最小占比",
            "type": "float",
            "default": 0.55,
            "min": 0.4,
            "max": 0.8,
            "step": 0.05,
        },
        {
            "id": "volume_ratio_min",
            "label": "最小放量倍数",
            "type": "float",
            "default": 1.5,
            "min": 1.0,
            "max": 3.0,
            "step": 0.1,
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
ENTRY_SIGNALS = ["dow_bottom_pregnancy_entry"]
EXIT_SIGNALS = ["dow_bottom_pregnancy_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("bottom_pregnancy", "buy", 2, ENTRY_SIGNALS[0])
