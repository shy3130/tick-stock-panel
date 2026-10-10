"""分钟红7 — 开盘 N 根 (当日最早) 分钟K多数收红, 且最高的 top_red 根全红。

数据契约: filter_minute_history 接收当日全市场分钟K窗口
(symbol, datetime, open, high, low, close, volume, amount),
由 ScreenerService.build_strategy_context 的 1m 分支从本地 kline_minute
分区注入; 策略本身不感知数据来源 (本地同步 / 盘中增量刷新对它透明)。

META["daily_history_bars"] 声明叠加日线维度的条件 (N 日内涨停过):
引擎会以 daily= 关键字注入日线 enriched 窗口, 涨停判定直接复用
enriched 预计算信号 — signal_limit_up (收盘封板) 或 signal_broken_limit_up
(炸板: 盘中触及涨停未封住), 任一命中即算"盘中涨停过"。

本文件是分钟红7的参考实现 (测试夹具): 该策略按用户要求以自定义策略形态
交付, 正式位置为运行时 data/strategies/custom/minute_red_streak.py
(gitignore, 用户可自行修改); 引擎按 id 加载, 参数覆盖不受位置影响。
"""

import polars as pl

META = {
    "id": "minute_red_streak",
    "name": "分钟红7",
    "description": "开盘前7根1分钟K至少5根收红, 最高的2根(按最高价)都是红K, 且近20日盘中触及过涨停",
    "tags": ["分钟", "形态", "短线"],
    "asset_types": ["stock"],
    "timeframes": ["1m"],
    # 日线 enriched 窗口 (交易日语义, 含 as_of): 覆盖 limit_up_days 参数上限
    "daily_history_bars": 60,
    "params": [
        {
            "id": "bars",
            "label": "开盘K线数",
            "type": "int",
            "default": 7,
            "min": 5,
            "max": 15,
            "step": 1,
        },
        {
            "id": "min_red",
            "label": "最少红K数",
            "type": "int",
            "default": 5,
            "min": 1,
            "max": 15,
            "step": 1,
        },
        {
            "id": "top_red",
            "label": "最高K需红数",
            "type": "int",
            "default": 2,
            "min": 1,
            "max": 3,
            "step": 1,
        },
        {
            "id": "rank_by_close",
            "label": "最高K按收盘价排序",
            "type": "bool",
            "default": False,
        },
        {
            "id": "require_limit_up",
            "label": "要求N日内涨停过",
            "type": "bool",
            "default": True,
        },
        {
            "id": "limit_up_days",
            "label": "涨停回看天数",
            "type": "int",
            "default": 20,
            "min": 5,
            "max": 60,
            "step": 1,
        },
    ],
    "order_by": "red_count",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS: list[str] = []
EXIT_SIGNALS: list[str] = []


def _recent_limit_ups(daily: pl.DataFrame | None, lookback: int) -> pl.DataFrame:
    """日线窗口 → (symbol, recent_limit_ups) 近 lookback 个交易日的涨停次数。

    涨停过 = signal_limit_up (收盘封板) 或 signal_broken_limit_up (炸板触及)。
    日线窗口缺失 / 无涨停信号列 → 返回空表 (调用方 inner join 即失败闭合,
    宁可漏过不可错报)。
    """
    empty = pl.DataFrame(schema={"symbol": pl.Utf8, "recent_limit_ups": pl.UInt32})
    if daily is None or daily.is_empty():
        return empty
    if not {"signal_limit_up", "signal_broken_limit_up"}.issubset(daily.columns):
        return empty
    return (
        daily.select("symbol", "date", "signal_limit_up", "signal_broken_limit_up")
        .sort(["symbol", "date"])
        .filter(pl.int_range(pl.len()).over("symbol") >= pl.len().over("symbol") - lookback)
        .group_by("symbol")
        .agg(
            recent_limit_ups=(
                pl.col("signal_limit_up").fill_null(False)
                | pl.col("signal_broken_limit_up").fill_null(False)
            ).sum()
        )
        .filter(pl.col("recent_limit_ups") > 0)
    )


def filter_minute_history(df: pl.DataFrame, params: dict, *, daily: pl.DataFrame | None = None) -> pl.DataFrame:
    """红K形态过滤: 全向量化, 无逐行 Python 循环。

    - 每标的按时间取当日最早 bars 根 (开盘窗口); 不足 bars 根不触发
    - 红 = close > open; 窗口内红K数 >= min_red
    - 按 rank_by (high / close) 降序取前 top_red 根, 同值取时间更晚者, 需全红
    - require_limit_up: 近 limit_up_days 个交易日盘中触及过涨停 (日线维度,
      由 daily 窗口的预计算涨停信号判定; 窗口缺失时失败闭合不触发)
    """
    bars = int(params.get("bars") or 7)
    min_red = min(int(params.get("min_red") or 5), bars)
    top_red = min(int(params.get("top_red") or 2), bars)
    rank_by = "close" if params.get("rank_by_close") else "high"
    if rank_by not in df.columns:
        rank_by = "high"

    windowed = (
        df.sort(["symbol", "datetime"])
        .filter(pl.int_range(pl.len()).over("symbol") < bars)
        .with_columns(_red=(pl.col("close") > pl.col("open")).cast(pl.Int32))
    )

    window = windowed.group_by("symbol").agg(
        bars_checked=pl.len(),
        red_count=pl.col("_red").sum(),
        last_datetime=pl.col("datetime").max(),
        # 输出列名用 close: 基础过滤的股价区间作用于开盘窗口末根收盘价
        close=pl.col("close").sort_by("datetime").last(),
        window_high=pl.col("high").max(),
        window_low=pl.col("low").min(),
        window_volume=pl.col("volume").sum(),
        window_amount=pl.col("amount").sum(),
    )

    top = (
        windowed.sort([rank_by, "datetime"], descending=[True, True])
        .filter(pl.int_range(pl.len()).over("symbol") < top_red)
        .group_by("symbol")
        .agg(top_red_count=pl.col("_red").sum())
    )

    result = (
        window.join(top, on="symbol", how="inner")
        .filter(
            (pl.col("bars_checked") >= bars)
            & (pl.col("red_count") >= min_red)
            & (pl.col("top_red_count") >= top_red)
        )
        .drop("bars_checked")
    )
    if params.get("require_limit_up", True):
        lookback = max(5, min(int(params.get("limit_up_days") or 20), 60))
        result = result.join(_recent_limit_ups(daily, lookback), on="symbol", how="inner")
    return result
