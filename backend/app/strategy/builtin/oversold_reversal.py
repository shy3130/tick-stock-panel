"""超跌反弹 — RSI14 < 30 + 涨幅 > 1% + 站上 MA5, 超卖反弹信号"""

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
    "id": "oversold_reversal",
    "name": "超跌反转",
    "description": "RSI14 < 30超卖 + 涨幅 > 1% + 站上MA5, 超卖反转信号",
    "tags": ["超跌", "反弹", "RSI"],
    "asset_types": ["stock"],
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
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 5.0,
            "step": 0.5,
        },
        {
            "id": "require_above_ma5",
            "label": "要求收盘价在MA5上方",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"change_pct": 0.4, "rsi_14": 0.3, "vol_ratio_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 15
ALERTS = [
    {"field": "rsi_14", "op": "<", "value": 25, "message": "RSI极度超卖"},
]


class OversoldReversalMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_rsi_filter", True):
            entry &= matrix_feature(market, "rsi_14") < float(params.get("rsi_max", 30.0))
        if params.get("use_change_filter", True):
            entry &= (
                matrix_feature(market, "change_pct") > float(params.get("min_change", 1.0)) / 100.0
            )
        if params.get("require_above_ma5", True):
            entry &= market.close > matrix_feature(market, "ma5")
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


MATRIX_STRATEGY = OversoldReversalMatrixStrategy()
