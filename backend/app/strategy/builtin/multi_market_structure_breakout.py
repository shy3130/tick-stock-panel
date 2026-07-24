"""A股、港股和美股共用的长期结构突破策略。"""

from app.strategy.shared_structure_breakout import SharedStructureBreakoutMatrixStrategy

META = {
    "id": "multi_market_structure_breakout",
    "name": "三市场结构突破",
    "description": "识别长期箱体、直角三角形和对称三角形的因果突破信号。",
    "tags": ["三市场", "结构", "突破", "趋势"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 0,
        "price_max": 1_000_000,
        "market_cap_min": 0,
        "amount_min": 0,
        "exclude_st": False,
        "exclude_new_days": 0,
    },
    "params": [
        {
            "id": "include_long_box",
            "label": "识别长期箱体",
            "type": "bool",
            "default": True,
        },
        {
            "id": "include_right_triangle",
            "label": "识别直角三角形",
            "type": "bool",
            "default": True,
        },
        {
            "id": "include_symmetric_triangle",
            "label": "识别对称三角形",
            "type": "bool",
            "default": True,
        },
        {
            "id": "min_breakout_volume_ratio",
            "label": "最小突破量比",
            "type": "float",
            "default": 1.5,
            "min": 1.0,
            "max": 3.0,
            "step": 0.1,
        },
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["shared_structure_breakout_entry"]
EXIT_SIGNALS = ["shared_structure_breakout_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60
ALERTS = []

class MultiMarketStructureBreakoutMatrixStrategy(SharedStructureBreakoutMatrixStrategy):
    """在内置策略模块中声明公式类型, 保持策略注册器的模块隔离约束。"""


MATRIX_STRATEGY = MultiMarketStructureBreakoutMatrixStrategy()
