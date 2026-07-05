"""高换手拉升 — 换手率 > 5% 且涨幅 > 3%, 资金活跃"""
import polars as pl

META = {
    "id": "high_turnover_surge",
    "name": "高换手拉升",
    "description": "换手率 > 5% 且涨幅 > 3%, 资金活跃",
    "tags": ["换手率", "放量", "资金"],
    "params": [
        {"id": "min_turnover", "label": "最低换手率%", "type": "float",
         "default": 5.0, "min": 1.0, "max": 20.0, "step": 0.5},
        {"id": "min_change", "label": "最低涨幅%", "type": "float",
         "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5},
    ],
    "scoring": {"turnover_rate": 0.4, "change_pct": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

ENTRY_SIGNALS = ["signal_volume_surge"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    min_to = params.get("min_turnover", 5.0)
    min_chg = params.get("min_change", 3.0) / 100.0
    return (
        (pl.col("turnover_rate") > min_to)
        & (pl.col("change_pct") > min_chg)
    )
