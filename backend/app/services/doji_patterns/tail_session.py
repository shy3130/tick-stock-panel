"""D5 tail-session three-shape detector using sealed minute data."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.services.hold_firm_patterns.models import (
    Bar,
    CensorReason,
    DetectionEvidence,
    MarketFactsSource,
)

from .models import (
    DOJI_BODY_RATIO_MAX,
    TAIL_BARE_BODY_RATIO_MIN,
    TAIL_OPEN_ANCHOR_MINUTE_INDEX,
    TAIL_SESSION_SHAPES,
    TAIL_VOLUME_SHARE_EXPAND_MIN,
    TAIL_VOLUME_SHARE_SHRINK_MAX,
    TAIL_WINDOW_MINUTE_INDICES,
    DojiDetection,
    DojiFactorId,
    DojiLandmark,
    DojiLandmarkKind,
)

FACTOR_ID: DojiFactorId = "tail_session_doji"


class TailMinuteSource(Protocol):
    rows: Mapping[tuple[str, date], object]
    unavailable: Mapping[tuple[str, date], str]


@dataclass(frozen=True, slots=True)
class TailCandle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_share: float
    body_ratio: float | None
    degenerate: bool


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _volume_state(share: float) -> str:
    if share <= TAIL_VOLUME_SHARE_SHRINK_MAX:
        return "shrink"
    if share >= TAIL_VOLUME_SHARE_EXPAND_MIN:
        return "expand"
    return "flat"


def _build_candle(day_row: object) -> TailCandle | None:
    raw_minutes = getattr(day_row, "minutes", None)
    if raw_minutes is None:
        return None
    by_index = {int(item.minute_index): item for item in raw_minutes}
    anchor = by_index.get(TAIL_OPEN_ANCHOR_MINUTE_INDEX)
    window = [by_index.get(index) for index in TAIL_WINDOW_MINUTE_INDICES]
    if anchor is None or any(item is None for item in window):
        return None
    opening = _finite(getattr(anchor, "close", None))
    closes = [_finite(getattr(item, "close", None)) for item in window]
    highs = [_finite(getattr(item, "high", None)) for item in window]
    lows = [_finite(getattr(item, "low", None)) for item in window]
    volumes = [_finite(getattr(item, "volume_shares", None)) for item in window]
    all_volumes = [_finite(getattr(item, "volume_shares", None)) for item in raw_minutes]
    if opening is None or any(x is None for x in closes + highs + lows + volumes + all_volumes):
        return None
    tail_volume = sum(x for x in volumes if x is not None)
    day_volume = sum(x for x in all_volumes if x is not None)
    high = max(x for x in highs if x is not None)
    low = min(x for x in lows if x is not None)
    close = closes[-1]
    if day_volume <= 0 or not all(math.isfinite(x) for x in (opening, high, low, close)):
        return None
    span = high - low
    body_ratio = abs(close - opening) / span if span > 0 else None
    return TailCandle(
        open=opening,
        high=high,
        low=low,
        close=close,
        volume=tail_volume,
        volume_share=tail_volume / day_volume,
        body_ratio=body_ratio,
        degenerate=span <= 0,
    )


def classify_tail_shape(candle: TailCandle, theta: float = DOJI_BODY_RATIO_MAX) -> str:
    """Return one of the three frozen anchors or ``other``."""
    if candle.body_ratio is not None:
        if candle.body_ratio >= TAIL_BARE_BODY_RATIO_MIN and candle.close > candle.open:
            return "bare_yang"
        if candle.body_ratio >= TAIL_BARE_BODY_RATIO_MIN and candle.close < candle.open:
            return "bare_yin"
        if candle.body_ratio <= theta and _volume_state(candle.volume_share) == "shrink":
            return "shrinking_doji"
    return "other"


class TailSessionDetector:
    def __init__(
        self, theta: float = DOJI_BODY_RATIO_MAX, source: TailMinuteSource | None = None
    ) -> None:
        self.theta = theta
        self.source = source

    @property
    def factor_id(self) -> DojiFactorId:
        return FACTOR_ID

    def detect(
        self, symbol: str, bars: Sequence[Bar], facts: MarketFactsSource, calendar: Sequence[date]
    ) -> tuple[DojiDetection, ...]:
        if self.source is None:
            return ()
        by_day = {bar.date: bar for bar in bars}
        out: list[DojiDetection] = []
        for index, day in enumerate(calendar):
            if day not in by_day or facts.row(symbol, day) is None:
                continue
            day_row = self.source.rows.get((symbol, day))
            if day_row is None:
                out.append(
                    DojiDetection(
                        FACTOR_ID, symbol, day, None, censor=CensorReason.MINUTE_DATA_INCOMPLETE
                    )
                )
                continue
            candle = _build_candle(day_row)
            if candle is None:
                out.append(
                    DojiDetection(
                        FACTOR_ID, symbol, day, None, censor=CensorReason.MINUTE_DATA_INCOMPLETE
                    )
                )
                continue
            if (
                index + 1 >= len(calendar)
                or calendar[index + 1] not in by_day
                or facts.row(symbol, calendar[index + 1]) is None
            ):
                out.append(
                    DojiDetection(
                        FACTOR_ID,
                        symbol,
                        day,
                        None,
                        censor=CensorReason.SELECTION_WINDOW_INCOMPLETE,
                    )
                )
                continue
            next_bar = by_day[calendar[index + 1]]
            next_direction = (
                "bullish"
                if next_bar.research_close_adj > next_bar.research_open_adj
                else "bearish"
                if next_bar.research_close_adj < next_bar.research_open_adj
                else "flat"
            )
            shape = classify_tail_shape(candle, self.theta)
            out.append(
                DojiDetection(
                    FACTOR_ID,
                    symbol,
                    day,
                    DojiLandmark(DojiLandmarkKind.SIGNAL_DAY_CLOSE, day, day),
                    evidence=DetectionEvidence(
                        shape in TAIL_SESSION_SHAPES,
                        {
                            "shape": shape,
                            "tail_open": candle.open,
                            "tail_high": candle.high,
                            "tail_low": candle.low,
                            "tail_close": candle.close,
                            "tail_body_ratio": candle.body_ratio,
                            "tail_volume": candle.volume,
                            "tail_volume_share": candle.volume_share,
                            "tail_volume_state": _volume_state(candle.volume_share),
                            "tail_degenerate": candle.degenerate,
                            "next_day_direction": next_direction,
                        },
                    ),
                )
            )
        return tuple(out)


__all__ = [
    "FACTOR_ID",
    "TailCandle",
    "TailMinuteSource",
    "TailSessionDetector",
    "classify_tail_shape",
]
