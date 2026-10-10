"""RSI 中轴回踩 — 强趋势中 RSI 回落至 50 中轴附近而不破, 低吸点"""

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
    "id": "rsi_midline_pullback",
    "name": "RSI 中轴回踩",
    "description": "多头趋势 (MA60 上方) 中 RSI 回落至 50 中轴区间企稳, 强势股低吸",
    "tags": ["RSI", "趋势", "低吸"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "rsi_period",
            "label": "RSI 周期",
            "type": "int",
            "default": 14,
            "min": 6,
            "max": 24,
            "step": 2,
        },
        {
            "id": "mid_low",
            "label": "中轴区间下沿",
            "type": "float",
            "default": 45.0,
            "min": 30.0,
            "max": 55.0,
            "step": 1.0,
        },
        {
            "id": "mid_high",
            "label": "中轴区间上沿",
            "type": "float",
            "default": 60.0,
            "min": 45.0,
            "max": 70.0,
            "step": 1.0,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "up_days_20d": 0.3, "vol_ratio_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_rsi_midline_pullback"]
EXIT_SIGNALS = ["signal_rsi_midline_fail"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class RSIMidlinePullbackMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        return int(params.get("rsi_period", 14)) + 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        rsi = matrix_feature(market, f"rsi_{int(params.get('rsi_period', 14))}")
        lo = float(params.get("mid_low", 45.0))
        hi = float(params.get("mid_high", 60.0))

        # 趋势前提: MA60 上方; 今日 RSI 落在中轴区间
        entry = market.close > matrix_feature(market, "ma60")
        entry &= (rsi >= lo) & (rsi <= hi)

        # 回踩而非走坏: 近5日内出现过 RSI > hi+5 (强势记忆), 且今日未破中轴下沿
        was_strong = shift(rsi, 1) > hi + 5
        for k in range(2, 6):
            was_strong |= shift(rsi, k) > hi + 5
        entry &= was_strong

        exit_ = market.close < matrix_feature(market, "ma20")
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_rsi_midline_pullback",),
            exit_signal_ids=("signal_rsi_midline_fail",),
        )


MATRIX_STRATEGY = RSIMidlinePullbackMatrixStrategy()
