"""单分钟放量拉升，一分钟自定义策略。

将本文件导入策略库，或放入 data/strategies/custom/ 后重新加载策略。
选择分钟周期运行，输入须来自已配置分钟数据源同步的分钟 K 线。

默认条件为单分钟涨幅至少 2%，且成交量至少为此前 20 根分钟均量的 3 倍。
涨幅相对上一根收盘价，均量不含当前根。窗口须连续，不跨午休或交易日。
只判断最新交易日每个标的最后一根已完成 K 线，当前分钟留待下一分钟评估。
本文件提供筛选计算，自动监控提醒需由应用的分钟监控入口另行接入。
"""

from datetime import datetime, time, timedelta, timezone

import polars as pl

META = {
    "id": "custom_minute_price_volume",
    "name": "单分钟放量拉升",
    "description": "单分钟涨幅≥2%，且分钟量≥此前20分钟均量的3倍",
    "tags": ["分钟", "放量", "拉升"],
    "asset_types": ["stock"],
    "timeframes": ["1m"],
    "basic_filter": {"enabled": False},
    "params": [
        {"id": "min_change_pct", "label": "单分钟涨幅下限(%)", "type": "float",
         "default": 2.0, "min": 0.0, "max": 30.0, "step": 0.1},
        {"id": "volume_window", "label": "均量窗口(分钟)", "type": "int",
         "default": 20, "min": 1, "max": 120, "step": 1},
        {"id": "volume_multiple", "label": "最低放量倍数", "type": "float",
         "default": 3.0, "min": 1.0, "max": 100.0, "step": 0.1},
    ],
    "scoring": {},
    "order_by": "minute_volume_ratio",
    "descending": True,
    "limit": 0,
}

EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []

RULES = """
1. 本分钟收盘价相对上一分钟收盘价上涨至少2%，使用同一价系。
2. 本分钟成交量至少为此前20根分钟K线平均成交量的3倍，均量不含本分钟。
3. 两项同时满足，且窗口连续、均量大于零，才输出该标的。
4. 午休或跨日后重新积累窗口；数据不足、无效或未完成的K线不触发。
"""


def filter_minute_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """返回每个标的最新已完成分钟的命中行，涨幅输出为小数比例。"""
    min_change_pct = float(params.get("min_change_pct", 2.0))
    window = int(params.get("volume_window", 20))
    volume_multiple = float(params.get("volume_multiple", 3.0))
    if not (0 <= min_change_pct <= 30 and 1 <= window <= 120 and 1 <= volume_multiple <= 100):
        raise ValueError("参数范围无效，请检查涨幅、均量窗口和放量倍数")

    empty = pl.DataFrame(schema={
        "symbol": pl.Utf8, "close": pl.Float64, "last_datetime": pl.Datetime,
        "minute_change_pct": pl.Float64, "minute_volume_ratio": pl.Float64,
        "previous_mean_volume": pl.Float64,
    })
    if df.is_empty():
        return empty
    if not {"symbol", "datetime", "close", "volume"}.issubset(df.columns):
        raise ValueError("分钟K线缺少 symbol/datetime/close/volume 必需列")

    timestamp = pl.col("datetime")
    if df.schema["datetime"].time_zone is not None:
        timestamp = timestamp.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    cutoff = datetime.now(timezone(timedelta(hours=8))).replace(
        tzinfo=None, second=0, microsecond=0,
    )
    bars = df.select(
        pl.col("symbol").cast(pl.Utf8), timestamp.alias("datetime"),
        pl.col("close").cast(pl.Float64, strict=False),
        pl.col("volume").cast(pl.Float64, strict=False),
    ).drop_nulls(["symbol", "datetime"]).filter(pl.col("datetime") < cutoff)
    bar_time = pl.col("datetime").dt.time()
    bars = bars.filter(
        bar_time.is_between(time(9, 30), time(11, 30))
        | bar_time.is_between(time(13, 0), time(15, 0))
    )
    if bars.is_empty():
        return empty

    bars = bars.with_columns(
        pl.col("datetime").dt.date().alias("date"),
        (pl.col("datetime").dt.hour() >= 13).alias("session"),
        pl.when(pl.col("volume").is_finite() & (pl.col("volume") >= 0))
        .then(pl.col("volume")).otherwise(None).alias("volume"),
    ).filter(pl.col("date") == pl.col("date").max())
    bars = bars.unique(subset=["symbol", "datetime"], keep="last").sort(["symbol", "datetime"])
    groups = ["symbol", "date", "session"]
    bars = bars.with_columns(
        pl.col("close").shift(1).over(groups).alias("previous_close"),
        pl.col("volume").shift(1).rolling_mean(window, min_samples=window)
        .over(groups).alias("previous_mean_volume"),
        pl.col("datetime").shift(window).over(groups).alias("window_start"),
    )

    # 先取最新根再筛选，避免把早先已失效的命中保留在当前结果中。
    latest = bars.group_by("symbol", maintain_order=True).tail(1)
    matched = latest.filter(
        pl.col("close").is_finite() & (pl.col("close") > 0)
        & pl.col("previous_close").is_finite() & (pl.col("previous_close") > 0)
        & (pl.col("previous_mean_volume") > 0)
        & (pl.col("datetime") - pl.col("window_start") == pl.duration(minutes=window))
        & (pl.col("close") >= pl.col("previous_close") * (1 + min_change_pct / 100))
        & (pl.col("volume") >= pl.col("previous_mean_volume") * volume_multiple)
    )
    return matched.select(
        "symbol", "close", pl.col("datetime").alias("last_datetime"),
        (pl.col("close") / pl.col("previous_close") - 1).alias("minute_change_pct"),
        (pl.col("volume") / pl.col("previous_mean_volume")).alias("minute_volume_ratio"),
        "previous_mean_volume",
    )
