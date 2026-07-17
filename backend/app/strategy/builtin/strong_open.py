"""强势高开 — 高开 > 3% 且保持上涨, 集合竞价强势"""

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
    "id": "strong_open",
    "name": "强势高开",
    "description": "高开 > 3% 且收盘高于开盘价, 集合竞价强势",
    "tags": ["高开", "强势"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_open_gap_filter", "label": "启用高开过滤", "type": "bool", "default": True},
        {
            "id": "min_open_gap",
            "label": "最低高开%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
        {
            "id": "require_close_above_open",
            "label": "要求收盘高于开盘",
            "type": "bool",
            "default": True,
        },
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
    ],
    "scoring": {"change_pct": 0.4, "amplitude": 0.2, "amount": 0.4},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
ALERTS = []


class StrongOpenMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_open_gap_filter", True):
            entry &= market.open > shift(market.close, 1) * (
                1.0 + float(params.get("min_open_gap", 3.0)) / 100.0
            )
        if params.get("require_close_above_open", True):
            entry &= market.close > market.open
        if params.get("use_change_filter", True):
            entry &= (
                matrix_feature(market, "change_pct") > float(params.get("min_change", 3.0)) / 100.0
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


MATRIX_STRATEGY = StrongOpenMatrixStrategy()
