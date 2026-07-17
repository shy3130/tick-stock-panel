"""量价齐升 — 突破MA20 + 放量 + 收阳"""

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
    "id": "volume_price_surge",
    "name": "量价齐升",
    "description": "突破MA20 + 放量 + 收阳",
    "tags": ["量价", "突破"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_ma20_breakout", "label": "要求突破MA20", "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 2.0,
            "min": 0.5,
            "max": 10.0,
            "step": 0.1,
        },
        {"id": "require_bullish_candle", "label": "要求收阳", "type": "bool", "default": True},
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_ma20_breakout"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
ALERTS = []


class VolumePriceSurgeMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma20 = matrix_feature(market, "ma20")
        breakout = (market.close > ma20) & (shift(market.close, 1) <= shift(ma20, 1))
        breakdown = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_ma20_breakout", True):
            entry &= breakout
        if params.get("use_volume_filter", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(
                params.get("vol_ratio_min", 2.0)
            )
        if params.get("require_bullish_candle", True):
            entry &= market.close > market.open
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=breakdown.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(breakdown, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_ma20_breakout",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = VolumePriceSurgeMatrixStrategy()
