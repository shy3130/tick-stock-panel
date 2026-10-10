"""长下影反击 — 超跌后放量收长下影线, 多头承接确认"""

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
    "id": "long_lower_shadow_reversal",
    "name": "长下影反击",
    "description": "近5日超跌后收长下影线且收盘收复实体, 下方承接强势的反转信号",
    "tags": ["K线形态", "超跌", "反转"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "shadow_pct_min",
            "label": "下影线最低长度% (相对昨收)",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
        {
            "id": "drop_pct_max",
            "label": "近5日累计跌幅下限% (负值)",
            "type": "float",
            "default": -5.0,
            "min": -30.0,
            "max": 0.0,
            "step": 1.0,
        },
        {"id": "require_recovery", "label": "要求收盘收复上半区", "type": "bool", "default": True},
    ],
    "scoring": {"momentum_20d": 0.3, "vol_ratio_5d": 0.4, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_long_lower_shadow"]
EXIT_SIGNALS = ["signal_ma5_lose_after_shadow"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10


class LongLowerShadowReversalMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 20

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        prev_close = matrix_feature(market, "prev_close")
        body_bot = np.minimum(market.open, market.close)
        shadow_pct = (body_bot - market.low) / prev_close * 100.0

        entry = shadow_pct >= float(params.get("shadow_pct_min", 3.0))
        entry &= matrix_feature(market, "momentum_5d") <= float(params.get("drop_pct_max", -5.0)) / 100.0
        entry &= matrix_feature(market, "vol_ratio_5d") >= 1.2   # 承接需有量
        if params.get("require_recovery", True):
            # 收盘位于当日振幅上半部 (close_position ∈ [0,1]): 长下影反击常为低开回拉,
            # 收盘仍可能低于开盘 (假阴线), 用位置而非阴阳判定收复力度
            entry &= matrix_feature(market, "close_position") >= 0.5

        ma5 = matrix_feature(market, "ma5")
        exit_ = market.close < ma5
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_long_lower_shadow",),
            exit_signal_ids=("signal_ma5_lose_after_shadow",),
        )


MATRIX_STRATEGY = LongLowerShadowReversalMatrixStrategy()
