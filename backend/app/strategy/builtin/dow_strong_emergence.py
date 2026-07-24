"""Dow strong-emergence buy point."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_strong_emergence",
    "name": "强势出现",
    "description": "底部区域出现放量大阳线并突破短期压力。",
    "tags": ["道式", "买点"],
    "strategy_role": "buy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "bottom_window",
            "label": "底部观察窗口",
            "type": "int",
            "default": 60,
            "min": 20,
            "max": 120,
            "step": 10,
        },
        {
            "id": "bottom_fraction",
            "label": "底部区域占比",
            "type": "float",
            "default": 0.35,
            "min": 0.15,
            "max": 0.5,
            "step": 0.05,
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
ENTRY_SIGNALS = ["dow_strong_emergence_entry"]
EXIT_SIGNALS = ["dow_strong_emergence_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("strong_emergence", "buy", 6, ENTRY_SIGNALS[0])
