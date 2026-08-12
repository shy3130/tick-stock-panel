"""趋势突破 — MA60上方 + 60日新高 + 放量"""

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
    "id": "trend_breakout",
    "name": "趋势突破",
    "description": "MA60上方 + 60日新高 + 量能 ≥ 2倍均量",
    "tags": ["趋势", "突破", "放量"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 5,
        "price_max": 200,
        "market_cap_min": 20e8,
        "amount_min": 1e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {
            "id": "require_above_ma60",
            "label": "要求收盘价在MA60上方",
            "type": "bool",
            "default": True,
        },
        {"id": "require_n_day_high", "label": "要求60日新高", "type": "bool", "default": True},
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
    ],
    "scoring": {"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_n_day_high"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20
ALERTS = [
    {"field": "signal_volume_surge", "message": "放量异动"},
]


class TrendBreakoutMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_above_ma60", True):
            entry &= market.close > matrix_feature(market, "ma60")
        if params.get("require_n_day_high", True):
            entry &= market.close >= matrix_feature(market, "high_60d")
        if params.get("use_volume_filter", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(
                params.get("vol_ratio_min", 2.0)
            )
        ma20 = matrix_feature(market, "ma20")
        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_n_day_high",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = TrendBreakoutMatrixStrategy()
