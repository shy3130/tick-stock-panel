"""Confirmed non-lookahead zigzag swing detection."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

KIND_HIGH = "high"
KIND_LOW = "low"


@dataclass(frozen=True, slots=True)
class ZigzagPivot:
    index: int
    price: float
    kind: str
    confirm_index: int


def confirmed_zigzag(
    highs: Sequence[float], lows: Sequence[float], threshold: float
) -> list[ZigzagPivot]:
    """Streaming zigzag; reversal threshold is inclusive and relative."""
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not math.isfinite(float(threshold))
        or threshold <= 0
    ):
        raise ValueError("threshold must be a positive finite number")
    if len(highs) != len(lows):
        raise ValueError("highs and lows must have equal length")
    for i, (high, low) in enumerate(zip(highs, lows, strict=False)):
        if not all(
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(float(v))
            and float(v) > 0
            for v in (high, low)
        ):
            raise ValueError(f"bar {i} has invalid high/low")
        if high < low:
            raise ValueError(f"bar {i} high < low")
    if not highs:
        return []
    threshold = float(threshold)
    pivots: list[ZigzagPivot] = []
    direction = 0
    hi_idx, hi_price = 0, float(highs[0])
    lo_idx, lo_price = 0, float(lows[0])
    for i, (high_raw, low_raw) in enumerate(zip(highs, lows, strict=False)):
        high, low = float(high_raw), float(low_raw)
        if direction >= 0 and high > hi_price:
            hi_idx, hi_price = i, high
            if direction == 0:
                lo_idx, lo_price = i, low
        if direction <= 0 and low < lo_price:
            lo_idx, lo_price = i, low
            if direction == 0:
                hi_idx, hi_price = i, high
        if direction == 0:
            if hi_idx < i and low <= hi_price * (1 - threshold):
                pivots.append(ZigzagPivot(hi_idx, hi_price, KIND_HIGH, i))
                direction, lo_idx, lo_price = -1, i, low
            elif lo_idx < i and high >= lo_price * (1 + threshold):
                pivots.append(ZigzagPivot(lo_idx, lo_price, KIND_LOW, i))
                direction, hi_idx, hi_price = 1, i, high
        elif direction > 0:
            if hi_idx < i and low <= hi_price * (1 - threshold):
                pivots.append(ZigzagPivot(hi_idx, hi_price, KIND_HIGH, i))
                direction, lo_idx, lo_price = -1, i, low
        else:
            if lo_idx < i and high >= lo_price * (1 + threshold):
                pivots.append(ZigzagPivot(lo_idx, lo_price, KIND_LOW, i))
                direction, hi_idx, hi_price = 1, i, high
    return pivots


__all__ = ["KIND_HIGH", "KIND_LOW", "ZigzagPivot", "confirmed_zigzag"]
