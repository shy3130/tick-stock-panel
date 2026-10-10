"""涨停基因活跃股 — 近期多次涨停的活跃标的池 (股性筛选, 非追板)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {
    "id": "active_limit_gene",
    "name": "涨停基因活跃股",
    "description": "近60日涨停次数达标的活跃标的, 且当日未涨停、缩量休整 — 股性筛选池",
    "tags": ["涨停", "活跃", "股性"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "min_limit_count",
            "label": "近60日最少涨停次数",
            "type": "int",
            "default": 3,
            "min": 2,
            "max": 10,
            "step": 1,
        },
        {
            "id": "vol_ratio_max",
            "label": "当日量比上限 (休整)",
            "type": "float",
            "default": 1.2,
            "min": 0.5,
            "max": 3.0,
            "step": 0.1,
        },
        {
            "id": "max_change_pct",
            "label": "当日涨幅上限% (排除涨停)",
            "type": "float",
            "default": 7.0,
            "min": 3.0,
            "max": 15.0,
            "step": 0.5,
        },
    ],
    "scoring": {"limit_up_count_60d": 0.4, "momentum_20d": 0.3, "turnover_ratio_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_active_limit_gene"]
EXIT_SIGNALS = ["signal_active_gene_cool"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 25


class ActiveLimitGeneMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 70

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = matrix_feature(market, "limit_up_count_60d") >= int(params.get("min_limit_count", 3))
        # 当日未涨停 (休整日而非情绪顶点) 且量能收敛
        entry &= matrix_feature(market, "change_pct") < float(params.get("max_change_pct", 7.0)) / 100.0
        entry &= matrix_feature(market, "vol_ratio_5d") <= float(params.get("vol_ratio_max", 1.2))

        # 出场: 股性冷却 (60日涨停计数回落到阈值下)
        exit_ = matrix_feature(market, "limit_up_count_60d") < int(params.get("min_limit_count", 3)) - 1
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_active_limit_gene",),
            exit_signal_ids=("signal_active_gene_cool",),
        )


MATRIX_STRATEGY = ActiveLimitGeneMatrixStrategy()
