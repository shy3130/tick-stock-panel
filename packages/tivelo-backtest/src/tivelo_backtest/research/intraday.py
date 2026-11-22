"""Legacy minute features with caller-supplied time zone and session grouping.

Volume remains lots (100 shares); amount and quote prices keep the v1 basis.
Session 0 is the opening session. The opening-range feature uses 30 observed
bars, not a verified exchange-time coverage proof. Sessions and completeness
must be supplied by the caller; this module contains no market calendar or I/O.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import polars as pl

# ── 特征白名单(供 custom_signals 校验与 /options 展示) ──────────
# 字段 → 中文标签。数值均为「每根已完成 bar 一个值」的序列。
INTRADAY_FEATURES: dict[str, str] = {
    "price": "现价",
    "vwap": "分时均价",
    "pct_vs_prev_close": "相对昨收涨跌幅",
    "pct_from_open": "相对开盘涨跌幅",
    "vol_ratio_1m_today": "1分钟放量比(今日基准)",
    "vol_ratio_3m_today": "3分钟放量比(今日基准)",
    "vol_ratio_5m_today": "5分钟放量比(今日基准)",
    "day_high_dist": "距当日最高价",
    "day_low_dist": "距当日最低价",
    "open_30m_high_dist": "距开盘30分钟最高价",
    "open_30m_low_dist": "距开盘30分钟最低价",
}

# 滚动窗口特征的窗口长度(字段名后缀 → bar 数)
_VOL_WINDOWS = {1: "vol_ratio_1m_today", 3: "vol_ratio_3m_today", 5: "vol_ratio_5m_today"}

_REQUIRED_COLS = ("symbol", "datetime", "close", "volume", "amount")
_DAY_KEY = ["symbol", "date"]
_SESSION_KEY = ["symbol", "date", "session"]


def cn_session_expr() -> pl.Expr:
    """Assign sessions in the existing A-share minute compatibility preset."""
    return pl.when(pl.col("datetime").dt.hour() >= 13).then(1).otherwise(0)


def _naive(dt: datetime, *, timezone: ZoneInfo) -> datetime | None:
    """统一为调用者声明的墙钟 naive; 不推断市场时区。"""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone).replace(tzinfo=None)
    return dt


def build_feature_frame(
    minute_df: pl.DataFrame,
    *,
    timezone: ZoneInfo,
    session_expr: pl.Expr,
    prev_close: dict[str, float] | None = None,
    cutoff: datetime | None = None,
) -> pl.DataFrame:
    """把分钟 K 帧编译为特征帧。

    参数:
      minute_df: 列含 symbol/datetime/open/high/low/close/volume/amount(后四列必需,
                 OHLC 缺失时相关特征降级为 null)。可包含多个交易日, 特征按日分组。
      prev_close: 映射 symbol → 昨收(已复权口径需与分钟价一致); 缺失标的的
                 pct_vs_prev_close 为 null。
      cutoff: 只使用严格早于 cutoff 的 bar(盘中传入当前分钟; 回放/回测传 None)。

    返回: symbol/datetime + INTRADAY_FEATURES 全部特征列(Float64, 可 null)。
    """
    empty = pl.DataFrame(
        schema={"symbol": pl.Utf8, "datetime": pl.Datetime, "date": pl.Date}
        | {name: pl.Float64 for name in INTRADAY_FEATURES}
    )
    if minute_df is None or minute_df.is_empty() or not set(_REQUIRED_COLS).issubset(minute_df.columns):
        return empty

    df = minute_df
    if "symbol" in df.columns:
        df = df.with_columns(pl.col("symbol").cast(pl.Utf8))
    dt_expr = pl.col("datetime")
    if df.schema["datetime"].time_zone is not None:
        dt_expr = dt_expr.dt.convert_time_zone(timezone.key).dt.replace_time_zone(None)
    df = df.with_columns(dt_expr.alias("datetime"))
    if cutoff is not None:
        cut = _naive(cutoff, timezone=timezone)
        if cut is not None:
            df = df.filter(pl.col("datetime") < cut)
    df = df.drop_nulls("datetime").sort(["symbol", "datetime"])
    if df.is_empty():
        return empty

    df = df.with_columns(
        pl.col("datetime").dt.date().alias("date"),
        # The caller assigns session groups after conversion to its wall clock.
        # Session 0 is the opening session used by the legacy 30-bar features.
        session_expr.alias("session"),
    )
    df = df.with_columns(pl.int_range(pl.len()).over(_SESSION_KEY).alias("session_idx"))

    cols = {"price": pl.col("close").cast(pl.Float64)}

    # ── 日累计特征(跨上午/下午累计) ──
    if {"volume", "amount"}.issubset(df.columns):
        cum_vol = pl.col("volume").cast(pl.Float64).cum_sum().over(_DAY_KEY)
        cum_amt = pl.col("amount").cast(pl.Float64).cum_sum().over(_DAY_KEY)
        cols["vwap"] = pl.when(cum_vol > 0).then(cum_amt / (cum_vol * 100.0))
    else:
        cols["vwap"] = pl.lit(None, dtype=pl.Float64)

    if prev_close:
        pc = pl.DataFrame(
            {"symbol": list(prev_close.keys()), "_prev_close": [float(v) for v in prev_close.values()]}
        )
        df = df.join(pc, on="symbol", how="left")
        cols["pct_vs_prev_close"] = pl.when(
            pl.col("_prev_close").is_not_null() & (pl.col("_prev_close") > 0)
        ).then(pl.col("close") / pl.col("_prev_close") - 1.0)
    else:
        cols["pct_vs_prev_close"] = pl.lit(None, dtype=pl.Float64)

    if "open" in df.columns:
        day_open = pl.col("open").cast(pl.Float64).first().over(_DAY_KEY)
        cols["pct_from_open"] = pl.when(day_open > 0).then(pl.col("close") / day_open - 1.0)
    else:
        cols["pct_from_open"] = pl.lit(None, dtype=pl.Float64)

    if "high" in df.columns:
        day_high = pl.col("high").cast(pl.Float64).cum_max().over(_DAY_KEY)
        cols["day_high_dist"] = pl.when(day_high > 0).then(pl.col("close") / day_high - 1.0)
    else:
        cols["day_high_dist"] = pl.lit(None, dtype=pl.Float64)

    if "low" in df.columns:
        day_low = pl.col("low").cast(pl.Float64).cum_min().over(_DAY_KEY)
        cols["day_low_dist"] = pl.when(day_low > 0).then(pl.col("close") / day_low - 1.0)
    else:
        cols["day_low_dist"] = pl.lit(None, dtype=pl.Float64)

    # ── 滚动放量比(今日基准): 当前 N 根 bar 量和 / 此前 N 根 bar 量和 ──
    # 滚动窗口按(symbol, date, session)分组 → 不跨午休、不跨日; 窗口不满自然为 null。
    if "volume" in df.columns:
        vol = pl.col("volume").cast(pl.Float64)
        for n, name in _VOL_WINDOWS.items():
            win = vol.rolling_sum(n).over(_SESSION_KEY)
            prev_win = win.shift(n).over(_SESSION_KEY)
            cols[name] = pl.when(prev_win > 0).then(win / prev_win)
    else:
        for name in _VOL_WINDOWS.values():
            cols[name] = pl.lit(None, dtype=pl.Float64)

    # ── 开盘 30 分钟高低点: 上午时段第 30 根 bar 的累计高/低, 全日广播 ──
    if "high" in df.columns:
        marker_h = (
            pl.when((pl.col("session") == 0) & (pl.col("session_idx") == 29))
            .then(pl.col("high").cast(pl.Float64).cum_max().over(_DAY_KEY))
            .otherwise(None)
            .forward_fill()
            .over(_DAY_KEY)
        )
        cols["open_30m_high_dist"] = pl.when(marker_h > 0).then(pl.col("close") / marker_h - 1.0)
    else:
        cols["open_30m_high_dist"] = pl.lit(None, dtype=pl.Float64)

    if "low" in df.columns:
        marker_l = (
            pl.when((pl.col("session") == 0) & (pl.col("session_idx") == 29))
            .then(pl.col("low").cast(pl.Float64).cum_min().over(_DAY_KEY))
            .otherwise(None)
            .forward_fill()
            .over(_DAY_KEY)
        )
        cols["open_30m_low_dist"] = pl.when(marker_l > 0).then(pl.col("close") / marker_l - 1.0)
    else:
        cols["open_30m_low_dist"] = pl.lit(None, dtype=pl.Float64)

    return df.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in cols.items()]).select(
        ["symbol", "date", "datetime", *INTRADAY_FEATURES.keys()]
    )
