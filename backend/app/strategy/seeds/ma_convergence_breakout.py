"""均线粘合突破 — MA5/10/20 挤压收敛后放量向上突破 (变盘启动点)"""

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
    "id": "ma_convergence_breakout",
    "name": "均线粘合突破",
    "description": "MA5/10/20 粘合收敛后放量突破, 挤压释放的变盘启动点",
    "tags": ["均线", "粘合", "突破"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "spread_pct_max",
            "label": "粘合带宽上限% (三线极差/MA20)",
            "type": "float",
            "default": 2.5,
            "min": 0.5,
            "max": 8.0,
            "step": 0.5,
        },
        {
            "id": "squeeze_days",
            "label": "粘合持续天数",
            "type": "int",
            "default": 5,
            "min": 3,
            "max": 15,
            "step": 1,
        },
        {
            "id": "min_change_pct",
            "label": "突破日最低涨幅%",
            "type": "float",
            "default": 2.0,
            "min": 0.0,
            "max": 10.0,
            "step": 0.5,
        },
        {"id": "require_volume", "label": "要求量比配合", "type": "bool", "default": True},
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_ma_convergence_breakout"]
EXIT_SIGNALS = ["signal_ma10_lose"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class MAConvergenceBreakoutMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        return 20 + int(params.get("squeeze_days", 5))

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma5 = matrix_feature(market, "ma5")
        ma10 = matrix_feature(market, "ma10")
        ma20 = matrix_feature(market, "ma20")
        band_top = np.maximum(np.maximum(ma5, ma10), ma20)
        band_bot = np.minimum(np.minimum(ma5, ma10), ma20)
        spread = np.abs(band_top - band_bot) / ma20 * 100.0

        # 粘合: 近 squeeze_days 日 (含昨日, 不含今日) 带宽持续低于阈值
        days = max(2, int(params.get("squeeze_days", 5)))
        tight = spread <= float(params.get("spread_pct_max", 2.5))
        squeeze = shift(tight, 1).astype(bool)
        for k in range(2, days + 1):
            squeeze &= shift(tight, k).astype(bool)

        # 今日变盘: 放量阳线脱离粘合带, 收盘站上三线上方
        entry = squeeze & (market.close > band_top)
        entry &= matrix_feature(market, "change_pct") >= float(params.get("min_change_pct", 2.0)) / 100.0
        if params.get("require_volume", True):
            entry &= matrix_feature(market, "vol_ratio_5d") >= 1.2

        exit_ = market.close < ma10
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_ma_convergence_breakout",),
            exit_signal_ids=("signal_ma10_lose",),
        )


MATRIX_STRATEGY = MAConvergenceBreakoutMatrixStrategy()
