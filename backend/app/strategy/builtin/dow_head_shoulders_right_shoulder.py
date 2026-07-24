"""Early Dow head-and-shoulders right-shoulder buy point."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_head_shoulders_right_shoulder",
    "name": "底部头肩右肩",
    "description": "潜在右肩回踩约50%位置并获得量价确认。",
    "tags": ["道式", "买点"],
    "strategy_role": "early_buy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "retrace_min",
            "label": "回踩下限",
            "type": "float",
            "default": 0.45,
            "min": 0.35,
            "max": 0.5,
            "step": 0.01,
        },
        {
            "id": "retrace_max",
            "label": "回踩上限",
            "type": "float",
            "default": 0.55,
            "min": 0.5,
            "max": 0.65,
            "step": 0.01,
        },
        {
            "id": "volume_ratio_min",
            "label": "最小量比",
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 2.0,
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
ENTRY_SIGNALS = ["dow_head_shoulders_right_shoulder_entry"]
EXIT_SIGNALS = ["dow_head_shoulders_right_shoulder_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy(
    "head_shoulders_right_shoulder", "early_buy", 20, ENTRY_SIGNALS[0]
)
