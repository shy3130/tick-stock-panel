"""指数多级别缠论结构分析。"""
from __future__ import annotations

from datetime import date, datetime, time
from importlib.metadata import PackageNotFoundError, version
from itertools import pairwise
from math import isfinite
from typing import Any

import polars as pl

try:
    import czsc as _czsc
except ImportError:  # 可选 extra; 默认安装保持轻量
    _czsc = None


_LEVELS = (
    ("daily", "日线", None),
    ("weekly", "周线", "1w"),
    ("monthly", "月线", "1mo"),
)
_MINUTE_LEVELS = (
    (1, "1f", "1F"),
    (5, "5f", "5F"),
    (10, "10f", "10F"),
    (15, "15f", "15F"),
    (30, "30f", "30F"),
    (60, "60f", "60F"),
    (120, "120f", "120F"),
)
_REQUIRED = {"date", "open", "high", "low", "close"}


def _bars_for_level(df: pl.DataFrame, every: str | None) -> list[dict[str, Any]]:
    columns = ["date", "open", "high", "low", "close"]
    optional = [name for name in ("volume", "amount") if name in df.columns]
    work = (
        df.select(columns + optional)
        .with_columns(
            pl.col("date").cast(pl.Date, strict=False),
            *[pl.col(name).cast(pl.Float64, strict=False) for name in columns[1:] + optional],
        )
        .drop_nulls(columns)
        .sort("date")
        .unique("date", keep="last", maintain_order=True)
    )
    if every:
        work = (
            work.with_columns(pl.col("date").dt.truncate(every).alias("_period"))
            .group_by("_period", maintain_order=True)
            .agg(
                pl.col("date").last(),
                pl.col("open").first(),
                pl.col("high").max(),
                pl.col("low").min(),
                pl.col("close").last(),
                *[pl.col(name).sum() for name in optional],
            )
            .drop("_period")
        )
    return work.to_dicts()


def _format_dt(value: date | datetime, minute: bool) -> str:
    return value.strftime("%Y-%m-%d %H:%M" if minute else "%Y-%m-%d")


def _builtin_pens(bars: list[dict[str, Any]], minute: bool = False) -> list[dict[str, Any]]:
    fractals: list[tuple[int, str, float]] = []
    for index in range(1, len(bars) - 1):
        prev, current, nxt = bars[index - 1], bars[index], bars[index + 1]
        if current["high"] > prev["high"] and current["high"] > nxt["high"]:
            kind, value = "top", float(current["high"])
        elif current["low"] < prev["low"] and current["low"] < nxt["low"]:
            kind, value = "bottom", float(current["low"])
        else:
            continue
        if fractals and fractals[-1][1] == kind:
            old = fractals[-1]
            if (kind == "top" and value > old[2]) or (kind == "bottom" and value < old[2]):
                fractals[-1] = (index, kind, value)
        elif not fractals or index - fractals[-1][0] >= 4:
            fractals.append((index, kind, value))

    return [
        {
            "start": _format_dt(bars[left[0]]["date"], minute),
            "start_value": left[2],
            "end": _format_dt(bars[right[0]]["date"], minute),
            "end_value": right[2],
            "direction": "up" if right[2] > left[2] else "down",
        }
        for left, right in pairwise(fractals)
    ]


def _czsc_pens(
    bars: list[dict[str, Any]], symbol: str, freq_name: str, minute: bool = False,
) -> list[dict[str, Any]]:
    freq = {
        "daily": _czsc.Freq.D, "weekly": _czsc.Freq.W, "monthly": _czsc.Freq.M,
        "1f": _czsc.Freq.F1, "5f": _czsc.Freq.F5, "10f": _czsc.Freq.F10,
        "15f": _czsc.Freq.F15, "30f": _czsc.Freq.F30, "60f": _czsc.Freq.F60,
        "120f": _czsc.Freq.F120,
    }[freq_name]
    raw = [
        _czsc.RawBar(
            symbol,
            row["date"] if isinstance(row["date"], datetime) else datetime.combine(row["date"], time()),
            freq,
            float(row["open"]),
            float(row["close"]),
            float(row["high"]),
            float(row["low"]),
            float(row.get("volume") or 0),
            float(row.get("amount") or 0),
            index,
        )
        for index, row in enumerate(bars)
    ]
    analyzer = _czsc.CZSC(raw, max_bi_num=200)
    return [
        {
            "start": _format_dt(bi.sdt, minute),
            "start_value": float(bi.fx_a.fx),
            "end": _format_dt(bi.edt, minute),
            "end_value": float(bi.fx_b.fx),
            "direction": "up" if str(bi.direction) in {"向上", "Up"} else "down",
        }
        for bi in analyzer.bi_list
    ]


def _centers(pens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    centers: list[dict[str, Any]] = []
    for index in range(len(pens) - 2):
        window = pens[index : index + 3]
        upper = min(max(pen["start_value"], pen["end_value"]) for pen in window)
        lower = max(min(pen["start_value"], pen["end_value"]) for pen in window)
        if lower >= upper:
            continue
        current = {
            "start": window[0]["start"],
            "end": window[-1]["end"],
            "upper": upper,
            "lower": lower,
        }
        if centers and current["start"] <= centers[-1]["end"]:
            merged_lower = max(centers[-1]["lower"], lower)
            merged_upper = min(centers[-1]["upper"], upper)
            if merged_lower < merged_upper:
                centers[-1].update(end=current["end"], lower=merged_lower, upper=merged_upper)
                continue
        centers.append(current)
    return centers


def _engine() -> str:
    if _czsc is None:
        return "builtin"
    try:
        return f"czsc-{version('czsc')}"
    except PackageNotFoundError:
        return "czsc"


def _analyze_bars(
    bars: list[dict[str, Any]], symbol: str, key: str, label: str, minute: bool = False,
) -> dict[str, Any]:
    if len(bars) < 3:
        pens = []
    elif _czsc is not None:
        pens = _czsc_pens(bars, symbol, key, minute)
    else:
        pens = _builtin_pens(bars, minute)
    return {
        "key": key,
        "label": label,
        "bars": [
            {
                **row,
                "date": _format_dt(row["date"], minute),
                **{
                    name: float(value)
                    for name, value in row.items()
                    if name != "date" and value is not None and isfinite(float(value))
                },
            }
            for row in bars
        ],
        "pens": pens,
        "centers": _centers(pens),
        "direction": pens[-1]["direction"] if pens else "flat",
    }


def _alignment(levels: list[dict[str, Any]]) -> str:
    directions = [level["direction"] for level in levels]
    return directions[0] if directions and "flat" not in directions and len(set(directions)) == 1 else "mixed"


def resample_minute(df: pl.DataFrame, period: int) -> pl.DataFrame:
    """按 A 股交易分钟序号合成 K 线,午休不计入周期。"""
    required = {"datetime", "open", "high", "low", "close"}
    if df.is_empty() or not required.issubset(df.columns):
        return pl.DataFrame()
    optional = [name for name in ("volume", "amount") if name in df.columns]
    clock = pl.col("datetime").dt.hour().cast(pl.Int16) * 60 + pl.col("datetime").dt.minute()
    offset = (
        pl.when(clock.is_between(570, 690)).then(clock - 570)
        .when(clock.is_between(780, 900)).then(pl.max_horizontal(clock - 660, pl.lit(121)))
    )
    work = (
        df.select([name for name in ("symbol", "datetime", "open", "high", "low", "close", *optional) if name in df.columns])
        .with_columns(
            pl.col("datetime").cast(pl.Datetime("us"), strict=False),
            *[pl.col(name).cast(pl.Float64, strict=False) for name in ("open", "high", "low", "close", *optional)],
        )
        .drop_nulls(["datetime", "open", "high", "low", "close"])
        .sort("datetime")
        .unique([name for name in ("symbol", "datetime") if name in df.columns], keep="last", maintain_order=True)
        .with_columns(offset.alias("_offset"))
        .drop_nulls("_offset")
        .with_columns(
            pl.col("datetime").dt.date().alias("_date"),
            ((pl.max_horizontal(pl.col("_offset"), pl.lit(1)) + period - 1) // period).alias("_bucket"),
        )
    )
    return (
        work.group_by("_date", "_bucket", maintain_order=True)
        .agg(
            *([pl.col("symbol").first()] if "symbol" in work.columns else []),
            pl.col("datetime").last(), pl.col("open").first(), pl.col("high").max(),
            pl.col("low").min(), pl.col("close").last(),
            *[pl.col(name).sum() for name in optional],
        )
        .drop("_date", "_bucket")
        .sort("datetime")
    )


def analyze_minute_levels(
    frames: dict[int, tuple[pl.DataFrame, str, str]], symbol: str,
) -> dict[str, Any]:
    """返回 1F 至 120F 分钟缠论结构及其直取/合成来源。"""
    levels = []
    for period, key, label in _MINUTE_LEVELS:
        df, source, source_period = frames.get(period, (pl.DataFrame(), "none", "--"))
        bars = []
        if not df.is_empty() and {"datetime", "open", "high", "low", "close"}.issubset(df.columns):
            bars = (
                df.select([name for name in ("datetime", "open", "high", "low", "close", "volume", "amount") if name in df.columns])
                .rename({"datetime": "date"})
                .sort("date")
                .to_dicts()
            )
        level = _analyze_bars(bars, symbol, key, label, minute=True)
        level.update(source=source, source_period=source_period)
        levels.append(level)
    return {"symbol": symbol, "engine": _engine(), "alignment": _alignment(levels), "levels": levels}


def analyze_levels(df: pl.DataFrame, symbol: str) -> dict[str, Any]:
    """返回日、周、月三级 K 线及笔和中枢实体。"""
    missing = _REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"缠论分析缺少字段: {', '.join(sorted(missing))}")

    levels = []
    for key, label, every in _LEVELS:
        bars = _bars_for_level(df, every)
        levels.append(_analyze_bars(bars, symbol, key, label))
    return {"symbol": symbol, "engine": _engine(), "alignment": _alignment(levels), "levels": levels}
