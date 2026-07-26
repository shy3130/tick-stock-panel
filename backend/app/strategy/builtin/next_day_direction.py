"""次日方向 — 趋势、量能、动量和技术共振。"""

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
    "id": "next_day_direction",
    "name": "次日方向",
    "description": "综合趋势、MACD、RSI、量价和突破状态，筛选次日偏多候选",
    "tags": ["次日", "概率", "趋势", "量价"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 500,
        "market_cap_min": 10e8,
        "amount_min": 0.3e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {
            "id": "min_probability",
            "label": "最低概率",
            "type": "float",
            "default": 70.0,
            "min": 50.0,
            "max": 95.0,
            "step": 1.0,
        },
        {
            "id": "require_positive_momentum",
            "label": "要求20日动量为正",
            "type": "bool",
            "default": True,
        },
        {
            "id": "avoid_overheated_rsi",
            "label": "排除RSI过热",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {
        "momentum_20d": 0.32,
        "momentum_60d": 0.24,
        "vol_ratio_5d": 0.18,
        "change_pct": 0.16,
        "turnover_rate": 0.10,
    },
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_next_day_direction"]
EXIT_SIGNALS = ["signal_macd_dead", "signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 5
ALERTS = [
    {"field": "signal_macd_golden", "message": "MACD金叉增强"},
    {"field": "vol_ratio_5d", "op": ">", "value": 1.5, "message": "量能放大"},
]


def _between(value: np.ndarray, low: float, high: float) -> np.ndarray:
    return np.isfinite(value) & (value >= low) & (value <= high)


class NextDayDirectionMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume", "amount", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 80

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        ma5 = matrix_feature(market, "ma5")
        ma20 = matrix_feature(market, "ma20")
        ma60 = matrix_feature(market, "ma60")
        momentum_20d = matrix_feature(market, "momentum_20d")
        momentum_60d = matrix_feature(market, "momentum_60d")
        vol_ratio = matrix_feature(market, "vol_ratio_5d")
        rsi = matrix_feature(market, "rsi_14")
        macd_hist = matrix_feature(market, "macd_hist")
        high_60d = matrix_feature(market, "high_60d")

        prev_close = shift(close, 1)
        prev_ma20 = shift(ma20, 1)
        prev_macd_hist = shift(macd_hist, 1)

        trend_ok = (close > ma20) & (ma20 >= ma60)
        short_trend_ok = (close > ma5) & (ma5 >= ma20)
        momentum_ok = (momentum_20d > 0) & (momentum_60d > -0.03)
        volume_ok = _between(vol_ratio, 1.05, 3.5)
        rsi_ok = _between(rsi, 45.0, 78.0)
        macd_ok = (macd_hist > 0) | ((macd_hist > prev_macd_hist) & (macd_hist > -0.03))
        breakout_ok = (close >= high_60d * 0.98) | ((close > ma20) & (prev_close <= prev_ma20))
        candle_ok = close > market.open

        points = (
            trend_ok.astype(np.int16) * 18
            + short_trend_ok.astype(np.int16) * 12
            + momentum_ok.astype(np.int16) * 18
            + volume_ok.astype(np.int16) * 14
            + rsi_ok.astype(np.int16) * 12
            + macd_ok.astype(np.int16) * 16
            + breakout_ok.astype(np.int16) * 10
        )
        if params.get("require_positive_momentum", True):
            points = np.where(momentum_20d > 0, points, points - 18)
        if params.get("avoid_overheated_rsi", True):
            points = np.where(rsi > 82.0, points - 18, points)
        points = np.where(candle_ok, points + 4, points)

        min_probability = float(params.get("min_probability", 70.0))
        entry = (points >= min_probability) & np.isfinite(close)
        exit_ = (close < ma20) | ((macd_hist < 0) & (prev_macd_hist >= 0))

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_next_day_direction",),
            exit_signal_ids=("signal_next_day_risk",),
        )


MATRIX_STRATEGY = NextDayDirectionMatrixStrategy()
