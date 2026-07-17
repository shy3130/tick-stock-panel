"""新低反转 — 60日新低后收阳放量"""

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
    "id": "n_day_low_reversal",
    "name": "新低反转",
    "description": "触及60日新低后当日收阳放量, 反转信号",
    "tags": ["反转", "新低"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_n_day_low", "label": "要求60日新低", "type": "bool", "default": True},
        {"id": "require_bullish_candle", "label": "要求收阳", "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 1.5,
            "min": 0.5,
            "max": 5.0,
            "step": 0.1,
        },
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_n_day_low"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
ALERTS = []


class NDayLowReversalMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_n_day_low", True):
            entry &= market.close <= matrix_feature(market, "low_60d")
        if params.get("require_bullish_candle", True):
            entry &= market.close > market.open
        if params.get("use_volume_filter", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(
                params.get("vol_ratio_min", 1.5)
            )
        ma20 = matrix_feature(market, "ma20")
        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_n_day_low",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = NDayLowReversalMatrixStrategy()
