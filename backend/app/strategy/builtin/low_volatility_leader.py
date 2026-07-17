"""低波动龙头 — 正动量 + 低波动 + MA20上方"""

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
    "id": "low_volatility_leader",
    "name": "低波动龙头",
    "description": "20日动量为正 + 年化波动 < 30% + MA20上方",
    "tags": ["低波动", "龙头"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "require_positive_momentum",
            "label": "要求20日动量为正",
            "type": "bool",
            "default": True,
        },
        {"id": "use_volatility_filter", "label": "启用波动率过滤", "type": "bool", "default": True},
        {
            "id": "vol_max",
            "label": "最大年化波动",
            "type": "float",
            "default": 0.30,
            "min": 0.05,
            "max": 1.0,
            "step": 0.01,
        },
        {
            "id": "require_above_ma20",
            "label": "要求收盘价在MA20上方",
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
ENTRY_SIGNALS = ["signal_ma20_breakout"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 30
ALERTS = []


class LowVolatilityLeaderMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma20 = matrix_feature(market, "ma20")
        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_positive_momentum", True):
            entry &= matrix_feature(market, "momentum_20d") > 0
        if params.get("use_volatility_filter", True):
            entry &= matrix_feature(market, "annual_vol_20d") < float(params.get("vol_max", 0.30))
        if params.get("require_above_ma20", True):
            entry &= market.close > ma20
        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_ma20_breakout",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = LowVolatilityLeaderMatrixStrategy()
