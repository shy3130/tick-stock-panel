"""逼近涨停 — 涨幅 > 7% 且距涨停 < 3%, 盘后选股"""
import polars as pl


def _limit_pct() -> pl.Expr:
    """根据板块和 ST 动态计算涨跌幅限制 (小数)。
    创业板(300/301)/科创板(688): 20% (含其 ST)
    北交所(.BJ): 30%
    主板 ST: 5%  ← ST 5% 仅主板生效, 创业板/科创板 ST 仍是 20%
    主板普通: 10%
    """
    is_st = pl.col("name").str.contains("(?i)ST").fill_null(False)
    is_cyb = pl.col("symbol").str.starts_with("300") | pl.col("symbol").str.starts_with("301")
    is_kcb = pl.col("symbol").str.starts_with("688")
    is_bj = pl.col("symbol").str.contains(r"\.BJ$")
    return (
        # 板块判定优先于 ST: 创业板/科创板 ST 保留 20%, 北交所 30%; ST 5% 只剩主板
        pl.when(is_cyb | is_kcb).then(0.20)
        .when(is_bj).then(0.30)
        .when(is_st).then(0.05)
        .otherwise(0.10)
    )


META = {
    "id": "near_limit_up",
    "name": "逼近涨停",
    "description": "涨幅 > 7% 且距涨停 < 3%, 追涨信号",
    "tags": ["涨停", "追涨"],
    "params": [
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool",
         "default": True},
        {"id": "min_change", "label": "最低涨幅%", "type": "float",
         "default": 7.0, "min": 3.0, "max": 15.0, "step": 1.0},
        {"id": "use_limit_gap_filter", "label": "启用距涨停空间过滤", "type": "bool",
         "default": True},
        {"id": "limit_gap", "label": "距涨停空间%", "type": "float",
         "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5},
    ],
    "scoring": {"change_pct": 0.5, "amount": 0.3, "momentum_5d": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    min_chg = params.get("min_change", 7.0) / 100.0
    gap = params.get("limit_gap", 3.0) / 100.0
    lp = _limit_pct()
    expr = pl.col("symbol").is_not_null() | pl.col("symbol").is_null()
    if params.get("use_change_filter", True):
        expr = expr & (pl.col("change_pct") > min_chg)
    if params.get("use_limit_gap_filter", True):
        expr = expr & (pl.col("change_pct") < lp - gap)
    return expr
