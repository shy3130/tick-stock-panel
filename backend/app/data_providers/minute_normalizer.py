"""行情边界的分钟字段和北京墙钟标准化;旧服务入口保留兼容导入。"""
from __future__ import annotations

import logging
from datetime import datetime

import polars as pl

logger = logging.getLogger(__name__)

CANONICAL_MINUTE_COLS = [
    "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
]


# 北京墙钟特征时段(含集合竞价 09:15 与收盘 15:00): 上午 09-11, 下午 13-15
_BJ_HOURS = [9, 10, 11, 13, 14, 15]
# 上述时段 -8h 的 UTC 墙钟特征: 上午 01-03, 下午 05-07
_UTC_SHIFTED_HOURS = [1, 2, 3, 5, 6, 7]


def _enforce_minute_beijing_wallclock(df: pl.DataFrame, *, source: str) -> pl.DataFrame:
    """分钟 K datetime 时区契约守卫: 统一为北京墙钟 (naive)。

    契约 (CONTRIBUTING §3.3): kline_minute.datetime 必须是北京时间墙钟, 如 09:35:00。
    在两个源头入口强制 —— _normalize_minute (TickFlow 帧) 与 _try_custom_minute
    (插件/自定义源帧); 落盘 (_write_minute_partition) 与内存消费 (监控/补拉/脉冲)
    均在其下游, 这里收口即全覆盖:
    - tz-aware → 转 Asia/Shanghai 后去时区;
    - naive 且时刻落在 A 股交易时段 → 直通 (已是北京墙钟);
    - naive 且整体呈"交易时段 -8h"的 UTC 特征 → 自动 +8 纠偏并记日志;
    - 无法识别的口径 → fail-closed 抛 ValueError, 不让脏时间入库或下发。
    幂等: 纠偏后的帧再过守卫直通, 不会二次改写。
    """
    if df.is_empty() or "datetime" not in df.columns:
        return df
    dtype = df.schema["datetime"]
    if not isinstance(dtype, pl.Datetime):
        # trade_time 等字符串路径: 先解析成 Datetime (失败置 null), 再做时段分类
        if dtype == pl.Utf8:
            df = df.with_columns(pl.col("datetime").str.to_datetime(strict=False))
        else:
            df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        dtype = df.schema["datetime"]
        if not isinstance(dtype, pl.Datetime):
            return df  # 仍非 Datetime: 维持原行为交由下游处理
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None:
        df = df.with_columns(
            pl.col("datetime")
            .dt.convert_time_zone("Asia/Shanghai")
            .dt.replace_time_zone(None)
            .cast(pl.Datetime("us"))
        )
        logger.info("minute datetime tz-aware input converted to Beijing wallclock (source=%s)", source)
        return df

    hour = pl.col("datetime").dt.hour()
    beijing = int(df.select(hour.is_in(_BJ_HOURS).sum()).item() or 0)
    utc_shifted = int(df.select(hour.is_in(_UTC_SHIFTED_HOURS).sum()).item() or 0)
    if beijing == 0 and utc_shifted == 0:
        if df["datetime"].null_count() == df.height:
            return df  # 全 null: 维持原行为, 由下游落盘过滤
        raise ValueError(
            f"minute datetime 口径无法识别 (source={source}, rows={df.height}, "
            f"sample={df['datetime'].drop_nulls().head(2).to_list()}): "
            "契约要求北京墙钟 (09:30-15:00), 既非交易时段也非 UTC 平移特征"
        )
    if utc_shifted > beijing:
        if beijing:
            logger.warning(
                "minute datetime mixed convention, shifting all by +8h per UTC majority "
                "(source=%s, utc=%d, beijing=%d)", source, utc_shifted, beijing,
            )
        else:
            logger.info(
                "minute datetime UTC wallclock detected, shifted +8h to Beijing "
                "(source=%s, rows=%d)", source, utc_shifted,
            )
        return df.with_columns(pl.col("datetime") + pl.duration(hours=8))
    if utc_shifted:
        logger.warning(
            "minute datetime has %d UTC-like rows among %d Beijing rows, left as-is "
            "(source=%s)", utc_shifted, beijing, source,
        )
    return df


def _normalize_minute(df_in, default_symbol: str | None = None, *, source: str = "tickflow") -> pl.DataFrame:
    """把 SDK 返回的分钟 K 数据规范成 canonical 列 (datetime 收口为北京墙钟)。"""
    if df_in is None or len(df_in) == 0:
        return pl.DataFrame()

    if not isinstance(df_in, pl.DataFrame):
        df = pl.from_pandas(df_in.reset_index() if hasattr(df_in, "reset_index") else df_in)
    else:
        df = df_in

    rename_map = {
        "ts_code": "symbol",
        "vol": "volume",
        "amt": "amount",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})

    # datetime 列:优先用 timestamp(毫秒精度),其次 trade_time
    if "timestamp" in df.columns:
        # TickFlow 毫秒时间戳为 UTC 基准; 契约要求北京墙钟 naive
        # (统一归一口径, 见 CONTRIBUTING §3.3)
        df = df.with_columns(
            pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="ms")
            .dt.replace_time_zone("UTC")
            .dt.convert_time_zone("Asia/Shanghai")
            .dt.replace_time_zone(None)
            .alias("datetime")
        ).drop("timestamp")
        for drop_col in ("trade_time", "trade_date"):
            if drop_col in df.columns:
                df = df.drop(drop_col)
    elif "trade_time" in df.columns:
        df = df.rename({"trade_time": "datetime"})
        if "trade_date" in df.columns:
            df = df.drop("trade_date")
    elif "trade_date" in df.columns:
        df = df.rename({"trade_date": "datetime"})

    if "datetime" in df.columns:
        # 时区契约守卫: 须在下方通用 us-cast 之前, 避免带时区列被静默剥成 UTC-naive
        df = _enforce_minute_beijing_wallclock(df, source=source)

    if "symbol" not in df.columns and default_symbol is not None:
        df = df.with_columns(pl.lit(default_symbol).alias("symbol"))

    # 类型规范:统一转 Datetime('us')
    if "datetime" in df.columns:
        dt_type = df.schema["datetime"]
        if not isinstance(dt_type, pl.Datetime) or dt_type.time_unit != "us":
            df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))

    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    for col in ("volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

    keep = [c for c in CANONICAL_MINUTE_COLS if c in df.columns]
    return df.select(keep)


def _compact_klines_to_df(raw, default_symbol: str | None = None) -> pl.DataFrame:
    """SDK as_dataframe=False 的 K 线原始数据 (CompactKlineData, 列式) → polars 直转。

    raw 两种形态: {symbol: Compact} (batch/universe) 或顶层 Compact (单标的)。
    列数组本身就是 polars 的原生形状 — 无 pandas 中转, 也无 SDK True 路径对
    每根 K 做的 datetime.fromtimestamp + trade_date/trade_time 字符串拼接。
    这里只搬运原始列 (timestamp 毫秒 Int64 + OHLCV), datetime/类型收口交给
    _normalize_minute / _normalize_daily — 与 True 路径同一契约。
    """
    if isinstance(raw, dict) and "timestamp" in raw:
        items: list[tuple[str | None, dict]] = [(None, raw)]
    elif isinstance(raw, dict):
        items = list(raw.items())
    else:
        return pl.DataFrame()
    frames: list[pl.DataFrame] = []
    for sym, kd in items:
        if not isinstance(kd, dict):
            continue
        ts = kd.get("timestamp") or []
        if not ts:
            continue
        key = sym if sym is not None else default_symbol
        cols: dict[str, object] = {"timestamp": ts}
        if key:
            cols["symbol"] = [key] * len(ts)
        for field in ("open", "high", "low", "close", "volume", "amount"):
            values = kd.get(field)
            if values is not None:
                cols[field] = values
        frames.append(pl.DataFrame(cols))
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def _timestamp_to_beijing_datetime(col: pl.Expr) -> pl.Expr:
    """timestamp (UTC 毫秒) → 北京墙钟 naive datetime 表达式。

    与 SDK True 路径 trade_date 列同口径: datetime.fromtimestamp(ts/1000, Asia/Shanghai)。
    """
    return (
        pl.from_epoch(col.cast(pl.Int64), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone("Asia/Shanghai")
        .dt.replace_time_zone(None)
    )


def _datetime_to_ms(dt: datetime) -> int:
    """datetime → 毫秒时间戳 (供 SDK start_time / end_time 使用)。"""
    return int(dt.timestamp() * 1000)
