"""布林突破 — 突破布林上轨 + 放量"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix, matrix_feature

META = {
    "id": "boll_breakout",
    "name": "布林突破",
    "description": "突破布林上轨 + 放量, 强势加速信号",
    "tags": ["布林", "突破"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "require_boll_breakout",
            "label": "要求突破布林上轨",
            "type": "bool",
            "default": True,
        },
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
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_boll_breakout_upper"]
EXIT_SIGNALS = ["signal_boll_breakdown_lower"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
ALERTS = []


class BollBreakoutMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        upper = matrix_feature(market, "boll_upper")
        lower = matrix_feature(market, "boll_lower")
        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_boll_breakout", True):
            entry &= market.close > upper
        if params.get("use_volume_filter", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(
                params.get("vol_ratio_min", 1.5)
            )
        exit_ = market.close < lower
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_boll_breakout_upper",),
            exit_signal_ids=("signal_boll_breakdown_lower",),
        )


MATRIX_STRATEGY = BollBreakoutMatrixStrategy()
