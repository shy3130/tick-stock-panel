"""超跌反弹 — RSI14 < 30 + 收阳 + 放量"""

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
    "id": "oversold_bounce",
    "name": "超跌反弹",
    "description": "RSI14 < 30超卖区 + 当日收阳 + 放量, 抄底信号",
    "tags": ["超跌", "反弹", "RSI"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_rsi_filter", "label": "启用RSI过滤", "type": "bool", "default": True},
        {
            "id": "rsi_max",
            "label": "RSI上限",
            "type": "float",
            "default": 30.0,
            "min": 10.0,
            "max": 50.0,
            "step": 1.0,
        },
        {"id": "require_bullish_candle", "label": "要求收阳", "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 1.2,
            "min": 0.5,
            "max": 5.0,
            "step": 0.1,
        },
    ],
    "scoring": {"change_pct": 0.3, "vol_ratio_5d": 0.3, "momentum_5d": 0.2, "rsi_14": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 15
ALERTS = [
    {"field": "rsi_14", "op": "<", "value": 25, "message": "RSI极度超卖"},
]


class OversoldBounceMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_rsi_filter", True):
            entry &= matrix_feature(market, "rsi_14") < float(params.get("rsi_max", 30.0))
        if params.get("require_bullish_candle", True):
            entry &= market.close > market.open
        if params.get("use_volume_filter", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(
                params.get("vol_ratio_min", 1.2)
            )
        ma20 = matrix_feature(market, "ma20")
        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = OversoldBounceMatrixStrategy()
