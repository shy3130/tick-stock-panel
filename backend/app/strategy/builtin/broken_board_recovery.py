"""断板反包 — 涨停 + 放量 + 涨幅 >3%"""

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
    "id": "broken_board_recovery",
    "name": "断板反包",
    "description": "连板≥2后断板1-2天, 出现放量反包信号",
    "tags": ["涨停", "反包"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_limit_up", "label": "要求当日涨停", "type": "bool", "default": True},
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
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "change_pct_min",
            "label": "最低涨幅",
            "type": "float",
            "default": 0.03,
            "min": 0.01,
            "max": 0.10,
            "step": 0.01,
        },
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_limit_up"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 10
ALERTS = []


class BrokenBoardRecoveryMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume", "raw_close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_limit_up", True):
            entry &= market.limit_up_locked.astype(bool)
        if params.get("use_volume_filter", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(
                params.get("vol_ratio_min", 1.5)
            )
        if params.get("use_change_filter", True):
            entry &= matrix_feature(market, "change_pct") > float(
                params.get("change_pct_min", 0.03)
            )
        ma20 = matrix_feature(market, "ma20")
        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_limit_up",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = BrokenBoardRecoveryMatrixStrategy()
