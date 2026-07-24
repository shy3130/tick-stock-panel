"""Dow right-triangle breakout."""

from app.strategy.shared_structure_breakout import SharedStructureBreakoutMatrixStrategy

META = {
    "id": "dow_right_triangle",
    "name": "直角三角型",
    "description": "水平压力位放量突破。",
    "tags": ["道式", "形态"],
    "strategy_role": "buy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "min_breakout_volume_ratio",
            "label": "最小突破量比",
            "type": "float",
            "default": 1.5,
            "min": 1.0,
            "max": 3.0,
            "step": 0.1,
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
ENTRY_SIGNALS = ["dow_right_triangle_entry"]
EXIT_SIGNALS = ["dow_right_triangle_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []
MATRIX_STRATEGY = SharedStructureBreakoutMatrixStrategy(fixed_pattern="right_triangle")
