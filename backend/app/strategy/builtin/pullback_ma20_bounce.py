"""均线回踩反弹 — 价格在 MA20 附近(±2%)且 MA 多头排列, 回踩买入"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "pullback_ma20_bounce",
    "name": "均线回踩反弹",
    "description": "价格在MA20附近(±2%)且MA5>MA20>MA60多头排列, 回踩买入",
    "tags": ["回踩", "均线", "反弹"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_ma20_proximity", "label": "启用MA20附近过滤", "type": "bool", "default": True},
        {
            "id": "ma_proximity",
            "label": "MA偏离度%",
            "type": "float",
            "default": 2.0,
            "min": 0.5,
            "max": 5.0,
            "step": 0.5,
        },
        {
            "id": "require_ma_alignment",
            "label": "要求MA5>MA20>MA60",
            "type": "bool",
            "default": True,
        },
        {"id": "require_positive_change", "label": "要求当日上涨", "type": "bool", "default": True},
    ],
    "scoring": {"momentum_60d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_ma_golden_5_20"]
EXIT_SIGNALS = ["signal_ma20_breakdown", "signal_ma_dead_5_20"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 15
ALERTS = []


class PullbackMA20BounceMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma5 = matrix_feature(market, "ma5")
        ma20 = matrix_feature(market, "ma20")
        ma60 = matrix_feature(market, "ma60")
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_ma20_proximity", True):
            proximity = float(params.get("ma_proximity", 2.0)) / 100.0
            entry &= (market.close > ma20 * (1.0 - proximity)) & (
                market.close < ma20 * (1.0 + proximity)
            )
        if params.get("require_ma_alignment", True):
            entry &= (ma5 > ma20) & (ma20 > ma60)
        if params.get("require_positive_change", True):
            entry &= matrix_feature(market, "change_pct") > 0
        ma20_breakdown = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        ma_dead = (ma5 < ma20) & (shift(ma5, 1) >= shift(ma20, 1))
        exit_ = ma20_breakdown | ma_dead
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(ma20_breakdown, 0, np.where(ma_dead, 1, -1)).astype(np.int16),
            entry_signal_ids=("signal_ma_golden_5_20",),
            exit_signal_ids=("signal_ma20_breakdown", "signal_ma_dead_5_20"),
        )


MATRIX_STRATEGY = PullbackMA20BounceMatrixStrategy()
