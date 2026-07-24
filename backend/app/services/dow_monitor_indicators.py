from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import polars as pl

from app.indicators.pipeline import compute_indicators

DOW_CHART_INDICATOR_FIELDS = (
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "rsi_6",
    "rsi_14",
    "rsi_24",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "boll_upper",
    "boll_lower",
    "vol_ma5",
    "vol_ma10",
    "vol_ratio_5d",
)


def enrich_dow_chart_bars(
    symbol: str,
    bars: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Add display-only indicators without changing the engine's OHLCV payload."""
    if not bars:
        return []

    source_rows = [dict(bar) for bar in bars]
    indicator_input = pl.DataFrame(
        [
            {
                "symbol": symbol,
                "date": bar["timestamp"],
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
                "_chart_position": position,
            }
            for position, bar in enumerate(source_rows)
        ]
    )
    computed_rows = (
        compute_indicators(
            indicator_input,
            needed=set(DOW_CHART_INDICATOR_FIELDS),
        )
        .sort("_chart_position")
        .to_dicts()
    )

    enriched: list[dict[str, object]] = []
    for source, computed in zip(source_rows, computed_rows, strict=True):
        row = dict(source)
        for field in DOW_CHART_INDICATOR_FIELDS:
            value = computed.get(field)
            row[field] = (
                float(value)
                if isinstance(value, (int, float)) and math.isfinite(value)
                else None
            )
        enriched.append(row)
    return enriched
