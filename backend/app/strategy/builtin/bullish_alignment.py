"""均线多头 — MA5>MA10>MA20>MA60 + 短期动量为正"""

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
    "id": "bullish_alignment",
    "name": "均线多头",
    "description": "MA5>MA10>MA20>MA60多头排列 + 短期动量为正",
    "tags": ["均线", "多头"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "require_ma_alignment",
            "label": "要求均线多头排列",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_positive_momentum",
            "label": "要求20日动量为正",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"momentum_60d": 0.4, "momentum_20d": 0.3, "turnover_rate": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_ma_golden_5_20", "signal_ma_golden_20_60"]
EXIT_SIGNALS = ["signal_ma_dead_5_20", "signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 20
ALERTS = []


class BullishAlignmentMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma5 = matrix_feature(market, "ma5")
        ma10 = matrix_feature(market, "ma10")
        ma20 = matrix_feature(market, "ma20")
        ma60 = matrix_feature(market, "ma60")
        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_ma_alignment", True):
            entry &= (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
        if params.get("require_positive_momentum", True):
            entry &= matrix_feature(market, "momentum_20d") > 0
        ma_dead = (ma5 < ma20) & (shift(ma5, 1) >= shift(ma20, 1))
        ma20_breakdown = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        exit_ = ma_dead | ma20_breakdown
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(ma_dead, 0, np.where(ma20_breakdown, 1, -1)).astype(np.int16),
            entry_signal_ids=("signal_ma_golden_5_20", "signal_ma_golden_20_60"),
            exit_signal_ids=("signal_ma_dead_5_20", "signal_ma20_breakdown"),
        )


MATRIX_STRATEGY = BullishAlignmentMatrixStrategy()
