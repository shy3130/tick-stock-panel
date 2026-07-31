"""逼近涨停 — 涨幅 > 7% 且距涨停 < 3%, 盘后选股"""

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
    "id": "near_limit_up",
    "name": "逼近涨停",
    "description": "涨幅 > 7% 且距涨停 < 3%, 追涨信号",
    "tags": ["涨停", "追涨"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 7.0,
            "min": 3.0,
            "max": 15.0,
            "step": 1.0,
        },
        {
            "id": "use_limit_gap_filter",
            "label": "启用距涨停空间过滤",
            "type": "bool",
            "default": True,
        },
        {
            "id": "limit_gap",
            "label": "距涨停空间%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
    ],
    "scoring": {"change_pct": 0.5, "amount": 0.3, "momentum_5d": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5
ALERTS = []


class NearLimitUpMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "price_limit_pct"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        change = matrix_feature(market, "change_pct")
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_change_filter", True):
            entry &= change > float(params.get("min_change", 7.0)) / 100.0
        if params.get("use_limit_gap_filter", True):
            limit_pct = matrix_feature(market, "price_limit_pct")
            entry &= (
                change
                >= limit_pct - float(params.get("limit_gap", 3.0)) / 100.0
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


MATRIX_STRATEGY = NearLimitUpMatrixStrategy()
