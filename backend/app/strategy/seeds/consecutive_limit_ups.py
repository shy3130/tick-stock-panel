"""连板股 — 涨停且连续涨停≥2天"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix, matrix_feature

META = {
    "id": "consecutive_limit_ups",
    "name": "连板股",
    "description": "当日涨停且连续涨停≥2天, 强势追涨",
    "tags": ["涨停", "连板"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_limit_up", "label": "要求当日涨停", "type": "bool", "default": True},
        {"id": "use_boards_filter", "label": "启用连板数过滤", "type": "bool", "default": True},
        {
            "id": "min_boards",
            "label": "最少连板数",
            "type": "int",
            "default": 2,
            "min": 1,
            "max": 20,
            "step": 1,
        },
    ],
    "scoring": {"consecutive_limit_ups": 0.5, "change_pct": 0.3, "amount": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_limit_up"]
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5


class ConsecutiveLimitUpsMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"consecutive_limit_ups", "raw_close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_limit_up", True):
            entry &= market.limit_up_locked.astype(bool)
        if params.get("use_boards_filter", True):
            entry &= matrix_feature(market, "consecutive_limit_ups") >= int(
                params.get("min_boards", 2)
            )
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_limit_up",),
        )


MATRIX_STRATEGY = ConsecutiveLimitUpsMatrixStrategy()
