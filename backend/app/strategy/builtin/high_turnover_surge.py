"""高换手拉升 — 换手率 > 5% 且涨幅 > 3%, 资金活跃"""

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
    "id": "high_turnover_surge",
    "name": "高换手拉升",
    "description": "换手率 > 5% 且涨幅 > 3%, 资金活跃",
    "tags": ["换手率", "放量", "资金"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_turnover_filter", "label": "启用换手率过滤", "type": "bool", "default": True},
        {
            "id": "min_turnover",
            "label": "最低换手率%",
            "type": "float",
            "default": 5.0,
            "min": 1.0,
            "max": 20.0,
            "step": 0.5,
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
    "scoring": {"turnover_rate": 0.4, "change_pct": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_volume_surge"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
ALERTS = []


class HighTurnoverSurgeMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_turnover_filter", True):
            entry &= matrix_feature(market, "turnover_rate") > float(
                params.get("min_turnover", 5.0)
            )
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
            entry_signal_ids=("signal_volume_surge",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = HighTurnoverSurgeMatrixStrategy()
