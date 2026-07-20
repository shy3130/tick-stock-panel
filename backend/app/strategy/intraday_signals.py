"""监控中心专用的日内分时穿越信号。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import polars as pl

from app.market_time import CN_TZ

INTRADAY_SIGNAL_LABELS: dict[str, str] = {
    "signal_intraday_avg_cross_up": "分时价格上穿均价",
    "signal_intraday_avg_cross_down": "分时价格下穿均价",
    "signal_intraday_zero_cross_up": "分时价格上穿0轴",
    "signal_intraday_zero_cross_down": "分时价格下穿0轴",
}
INTRADAY_SIGNAL_FIELDS = frozenset(INTRADAY_SIGNAL_LABELS)


def uses_intraday_signals(rule: dict) -> bool:
    return any(
        c.get("op") == "truth" and c.get("field") in INTRADAY_SIGNAL_FIELDS
        for c in rule.get("conditions", [])
        if isinstance(c, dict)
    )


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _naive_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(CN_TZ).replace(tzinfo=None)
    return value


class IntradaySignalEvaluator:
    """按已完成的一分钟 K 线生成边沿触发信号。"""

    def __init__(self) -> None:
        self._last_bar: dict[tuple[str, str], datetime] = {}

    def evaluate(
        self,
        minute_df: pl.DataFrame,
        *,
        symbols: set[str],
        prev_close: dict[str, float],
        asset_type: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        active_keys = {(asset_type, symbol) for symbol in symbols}
        self._last_bar = {
            key: value for key, value in self._last_bar.items()
            if key[0] != asset_type or key in active_keys
        }
        required = {"symbol", "datetime", "close", "volume", "amount"}
        if not symbols or minute_df.is_empty() or not required.issubset(minute_df.columns):
            return []

        cutoff = _naive_datetime(now)
        if cutoff is None:
            return []
        cutoff = cutoff.replace(second=0, microsecond=0)
        scoped = minute_df.filter(pl.col("symbol").cast(pl.Utf8).is_in(sorted(symbols)))
        if scoped.is_empty():
            return []

        results: list[dict[str, Any]] = []
        for part in scoped.partition_by("symbol", maintain_order=False):
            part = part.sort("datetime")
            symbol = str(part["symbol"][0])
            points: list[tuple[datetime, float, float | None]] = []
            cumulative_amount = 0.0
            cumulative_volume = 0.0
            for row in part.iter_rows(named=True):
                bar_time = _naive_datetime(row.get("datetime"))
                price = _finite(row.get("close"))
                volume = _finite(row.get("volume"))
                amount = _finite(row.get("amount"))
                if bar_time is None or bar_time.date() != cutoff.date() or bar_time >= cutoff or price is None:
                    continue
                if volume is not None and volume > 0 and amount is not None and amount >= 0:
                    cumulative_volume += volume
                    cumulative_amount += amount
                average = (
                    cumulative_amount / (cumulative_volume * 100.0)
                    if cumulative_volume > 0 and cumulative_amount > 0
                    else None
                )
                points.append((bar_time, price, average))

            if not points:
                continue
            current = points[-1]
            key = (asset_type, symbol)
            last_bar = self._last_bar.get(key)
            self._last_bar[key] = current[0]
            if last_bar is None or last_bar.date() != current[0].date() or current[0] <= last_bar:
                continue
            if len(points) < 2:
                continue

            previous = points[-2]
            baseline = _finite(prev_close.get(symbol))
            avg_up = previous[2] is not None and current[2] is not None and previous[1] <= previous[2] and current[1] > current[2]
            avg_down = previous[2] is not None and current[2] is not None and previous[1] >= previous[2] and current[1] < current[2]
            zero_up = baseline is not None and baseline > 0 and previous[1] <= baseline and current[1] > baseline
            zero_down = baseline is not None and baseline > 0 and previous[1] >= baseline and current[1] < baseline
            if avg_up or avg_down or zero_up or zero_down:
                results.append({
                    "symbol": symbol,
                    "signal_intraday_avg_cross_up": avg_up,
                    "signal_intraday_avg_cross_down": avg_down,
                    "signal_intraday_zero_cross_up": zero_up,
                    "signal_intraday_zero_cross_down": zero_down,
                })
        return results

    @staticmethod
    def inject(df: pl.DataFrame, signals: list[dict[str, Any]]) -> pl.DataFrame:
        existing = [field for field in INTRADAY_SIGNAL_FIELDS if field in df.columns]
        out = df.drop(existing) if existing else df
        if signals:
            out = out.join(pl.DataFrame(signals), on="symbol", how="left")
        else:
            out = out.with_columns([
                pl.lit(False).alias(field) for field in INTRADAY_SIGNAL_FIELDS
            ])
        return out.with_columns([
            pl.col(field).fill_null(False).cast(pl.Boolean).alias(field)
            for field in INTRADAY_SIGNAL_FIELDS
        ])
