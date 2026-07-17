"""缩量回踩 — 回踩MA20附近 + 缩量 + 中期趋势向上"""

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
    "id": "pullback_to_support",
    "name": "缩量回踩",
    "description": "回踩MA20附近 + 缩量 + 中期趋势向上",
    "tags": ["回踩", "支撑"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_ma20_proximity", "label": "启用MA20附近过滤", "type": "bool", "default": True},
        {
            "id": "ma_proximity",
            "label": "均线偏离度",
            "type": "float",
            "default": 0.02,
            "min": 0.01,
            "max": 0.05,
            "step": 0.005,
        },
        {"id": "use_volume_filter", "label": "启用缩量过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_max",
            "label": "最大量比",
            "type": "float",
            "default": 0.8,
            "min": 0.2,
            "max": 1.5,
            "step": 0.1,
        },
        {
            "id": "require_above_ma60",
            "label": "要求收盘价在MA60上方",
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
ENTRY_SIGNALS = ["signal_ma_golden_5_20"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20
ALERTS = []


class PullbackToSupportMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma20 = matrix_feature(market, "ma20")
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_ma20_proximity", True):
            proximity = float(params.get("ma_proximity", 0.02))
            entry &= (market.close > ma20 * (1.0 - proximity)) & (
                market.close < ma20 * (1.0 + proximity)
            )
        if params.get("use_volume_filter", True):
            entry &= matrix_feature(market, "vol_ratio_5d") < float(
                params.get("vol_ratio_max", 0.8)
            )
        if params.get("require_above_ma60", True):
            entry &= market.close > matrix_feature(market, "ma60")
        if params.get("require_positive_momentum", True):
            entry &= matrix_feature(market, "momentum_20d") > 0
        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_ma_golden_5_20",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = PullbackToSupportMatrixStrategy()
