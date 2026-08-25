"""断板反包 — 连板≥2 后断板 1~2 个交易日, 当日放量收阳反包 (未涨停)。

形态由 filter_history 在多日窗口上判定 (LOOKBACK_DAYS 是自然日窗口,
按 5 个交易日 + 周末/长假缓冲取 11):
    连续涨停 ≥2 天 → 断板 1~2 个交易日 (断板日非涨停) → as_of 当日
    收阳 (close>open) + 放量 (vol_ratio_5d 达标) + 涨幅达标, 且当日未涨停。
"""
import polars as pl

# 形态最深回看: 大前日 (d-3) 的连板段还需 ≥2 根涨停 → 共 5 个交易日。
# _load_enriched_history 的 lookback_days 是自然日 (start = as_of - days),
# 6 个自然日在周一/节前只覆盖 2-4 个交易日会截断连板段; 对齐 sequence
# 路径的换算口径 ceil(5*7/5)+4 = 11, 保证长假后也能取满 5 个交易日。
LOOKBACK_DAYS = 11

META = {
    "id": "broken_board_recovery",
    "name": "断板反包",
    "description": "连板≥2后断板1-2天, 出现放量反包信号",
    "tags": ["涨停", "反包"],
    "params": [
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
        {"id": "change_pct_min", "label": "最低涨幅", "type": "float",
         "default": 0.03, "min": 0.01, "max": 0.10, "step": 0.01},
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

# 当日反包条件 (收阳/放量/未涨停) 已由 filter() 全量表达;
# 不再叠加 signal_limit_up 之类的信号列做买点过滤, 避免回测买点与选股命中口径分裂。
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 10
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    """单日条件: 形态 setup + 收阳 + 放量 + 涨幅达标 + 当日未涨停。

    setup 列 __broken_board_setup 由 filter_history 计算写入。
    """
    vol_min = params.get("vol_ratio_min", 1.5)
    chg_min = params.get("change_pct_min", 0.03)
    return (
        pl.col("__broken_board_setup").fill_null(False)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("vol_ratio_5d") >= vol_min)
        & (pl.col("change_pct") > chg_min)
        & ~pl.col("signal_limit_up").fill_null(False)
    )


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """多日形态判定: 返回窗口内命中「断板反包」的行 (任意日期)。

    - 按 symbol, date 排序后用 .over("symbol") 行序窗口。
    - 对每一行 d 计算 setup (即 d 作为反包日的形态前提):
        断板 1 天: d-1 非涨停, 且截至 d-2 的连续涨停数 ≥2;
        断板 2 天: d-1、d-2 均非涨停, 且截至 d-3 的连续涨停数 ≥2。
      即「连板≥2 的涨停段在 1~2 个交易日前结束, 断板日都不是涨停」。
    - 返回行保留 __broken_board_setup 列, 供引擎 Stage-2 的 filter() 复核。
    """
    df = df.sort(["symbol", "date"])
    df = df.with_columns(
        pl.col("signal_limit_up").fill_null(False).alias("__is_lu")
    )
    # 连续同状态段编号: __is_lu 翻转处切段 (段内涨停状态一致)
    df = df.with_columns(
        (
            (pl.col("__is_lu") != pl.col("__is_lu").shift(1).over("symbol"))
            .fill_null(True)
            .cum_sum()
            .over("symbol")
        ).alias("__run_id")
    )
    # 截至当前行的连续涨停天数 (非涨停行 = 0)
    df = df.with_columns(
        pl.when(pl.col("__is_lu"))
        .then(pl.col("__is_lu").cast(pl.Int32).cum_sum().over(["symbol", "__run_id"]))
        .otherwise(pl.lit(0, dtype=pl.Int32))
        .alias("__lu_streak")
    )
    # 前方 1/2/3 个交易日的涨停状态与截至该日的连板数
    df = df.with_columns(
        pl.col("__is_lu").shift(1).over("symbol").fill_null(False).alias("__p1_lu"),
        pl.col("__is_lu").shift(2).over("symbol").fill_null(False).alias("__p2_lu"),
        pl.col("__lu_streak").shift(2).over("symbol").fill_null(0).alias("__streak_p2"),
        pl.col("__lu_streak").shift(3).over("symbol").fill_null(0).alias("__streak_p3"),
    )
    df = df.with_columns(
        (
            # 断板 1 天: 昨日非涨停, 前日是连板≥2 段的最后一板
            (~pl.col("__p1_lu") & (pl.col("__streak_p2") >= 2))
            # 断板 2 天: 前两日均非涨停, 大前日是连板≥2 段的最后一板
            | (~pl.col("__p1_lu") & ~pl.col("__p2_lu") & (pl.col("__streak_p3") >= 2))
        ).fill_null(False).alias("__broken_board_setup")
    )
    df = df.filter(filter(df, params))
    return df.drop(
        "__is_lu", "__run_id", "__lu_streak",
        "__p1_lu", "__p2_lu", "__streak_p2", "__streak_p3",
    )
