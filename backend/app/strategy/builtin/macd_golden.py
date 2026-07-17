"""MACD金叉放量 — MACD金叉当日 + 量能放大"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_ewm_adjust_false as ewm_adjust_false,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "macd_golden",
    "name": "MACD 金叉放量",
    "description": "MACD金叉当日 + 量能放大",
    "tags": ["MACD", "金叉", "放量"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_macd_golden", "label": "要求MACD金叉", "type": "bool", "default": True},
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
    "scoring": {"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_macd_golden"]
EXIT_SIGNALS = ["signal_macd_dead"]
EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.07
MAX_HOLD_DAYS = 20
ALERTS = []


class MACDGoldenMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        valid = np.isfinite(market.close)
        ema12 = ewm_adjust_false(market.close, valid, span=12)
        ema26 = ewm_adjust_false(market.close, valid, span=26)
        dif = ema12 - ema26
        dif_valid = np.isfinite(dif)
        dea = ewm_adjust_false(dif, dif_valid, span=9)
        previous_dif = shift(dif, 1, dif_valid)
        previous_dea = shift(dea, 1, np.isfinite(dea))
        golden = (dif > dea) & (previous_dif <= previous_dea)
        dead = (dif < dea) & (previous_dif >= previous_dea)

        entry = (
            golden
            if params.get("require_macd_golden", True)
            else np.ones(
                market.shape,
                dtype=bool,
            )
        )
        if params.get("use_volume_filter", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(
                params.get("vol_ratio_min", 1.5)
            )

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=dead.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(dead, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_macd_golden",),
            exit_signal_ids=("signal_macd_dead",),
        )


MATRIX_STRATEGY = MACDGoldenMatrixStrategy()
