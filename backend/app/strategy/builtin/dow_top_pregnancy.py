"""Old-note top pregnancy risk pattern."""

from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy

META = {
    "id": "dow_top_pregnancy",
    "name": "顶部孕线",
    "description": "小实体K线后出现放量大阴线，且高低点完全吞没前一根。",
    "tags": ["道式", "K线", "风险"],
    "strategy_role": "risk",
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
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["dow_top_pregnancy_risk"]
STOP_LOSS = None
MAX_HOLD_DAYS = None
ALERTS = []
MATRIX_STRATEGY = DowPatternMatrixStrategy("top_pregnancy", "risk", 2, EXIT_SIGNALS[0])
