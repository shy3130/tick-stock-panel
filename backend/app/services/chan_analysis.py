"""指数多级别缠论结构分析。"""
from __future__ import annotations

from datetime import datetime, time
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


def _builtin_pens(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            "start": str(bars[left[0]]["date"]),
            "start_value": left[2],
            "end": str(bars[right[0]]["date"]),
            "end_value": right[2],
            "direction": "up" if right[2] > left[2] else "down",
        }
        for left, right in pairwise(fractals)
    ]


def _czsc_pens(bars: list[dict[str, Any]], symbol: str, freq_name: str) -> list[dict[str, Any]]:
    freq = {"daily": _czsc.Freq.D, "weekly": _czsc.Freq.W, "monthly": _czsc.Freq.M}[freq_name]
    raw = [
        _czsc.RawBar(
            symbol,
            datetime.combine(row["date"], time()),
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
            "start": str(bi.sdt.date()),
            "start_value": float(bi.fx_a.fx),
            "end": str(bi.edt.date()),
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


def analyze_levels(df: pl.DataFrame, symbol: str) -> dict[str, Any]:
    """返回日、周、月三级 K 线及笔和中枢实体。"""
    missing = _REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"缠论分析缺少字段: {', '.join(sorted(missing))}")

    engine = "builtin"
    if _czsc is not None:
        try:
            engine = f"czsc-{version('czsc')}"
        except PackageNotFoundError:
            engine = "czsc"

    levels = []
    for key, label, every in _LEVELS:
        bars = _bars_for_level(df, every)
        pens = _czsc_pens(bars, symbol, key) if _czsc is not None else _builtin_pens(bars)
        centers = _centers(pens)
        levels.append(
            {
                "key": key,
                "label": label,
                "bars": [
                    {
                        **row,
                        "date": str(row["date"]),
                        **{
                            name: float(value)
                            for name, value in row.items()
                            if name != "date" and value is not None and isfinite(float(value))
                        },
                    }
                    for row in bars
                ],
                "pens": pens,
                "centers": centers,
                "direction": pens[-1]["direction"] if pens else "flat",
            }
        )

    directions = [level["direction"] for level in levels]
    alignment = directions[0] if "flat" not in directions and len(set(directions)) == 1 else "mixed"
    return {"symbol": symbol, "engine": engine, "alignment": alignment, "levels": levels}
