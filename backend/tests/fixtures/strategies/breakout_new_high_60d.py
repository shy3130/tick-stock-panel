"""放量创60日新高 — 突破前期高点平台 + 量能确认"""

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
    "id": "breakout_new_high_60d",
    "name": "放量创60日新高",
    "description": "收盘突破前60日最高价, 量比配合, 趋势强度确认",
    "tags": ["突破", "新高"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_volume", "label": "要求量比配合", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 1.3,
            "min": 0.5,
            "max": 5.0,
            "step": 0.1,
        },
        {
            "id": "min_change_pct",
            "label": "最低当日涨幅%",
            "type": "float",
            "default": 2.0,
            "min": 0.0,
            "max": 10.0,
            "step": 0.5,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_breakout_high_60d"]
EXIT_SIGNALS = ["signal_break_ma20_lose"]
STOP_LOSS = -0.07
MAX_HOLD_DAYS = 20


class BreakoutNewHigh60dMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 70

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        prior_high = shift(matrix_feature(market, "high_60d"), 1)   # 昨日及以前的60日高点
        entry = market.close > prior_high
        entry &= matrix_feature(market, "change_pct") >= float(params.get("min_change_pct", 2.0)) / 100.0
        if params.get("require_volume", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(params.get("vol_ratio_min", 1.3))
        ma20 = matrix_feature(market, "ma20")
        exit_ = market.close < ma20
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_breakout_high_60d",),
            exit_signal_ids=("signal_break_ma20_lose",),
        )


MATRIX_STRATEGY = BreakoutNewHigh60dMatrixStrategy()
