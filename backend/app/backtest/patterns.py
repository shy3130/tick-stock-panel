from __future__ import annotations

import math
from dataclasses import dataclass

import polars as pl


PIVOT_WINDOW = 5


@dataclass(frozen=True)
class PatternHit:
    pattern: str
    date: str
    confidence: float
    features: dict

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "date": self.date,
            "confidence": round(self.confidence, 4),
            "features": self.features,
        }


def detect_patterns(df: pl.DataFrame, lookback: int = 120) -> list[dict]:
    if df.is_empty():
        return []
    scoped = df.sort("date").tail(max(lookback, PIVOT_WINDOW))
    hits = []
    for hit in (detect_consolidation(scoped), detect_breakout(scoped), detect_double_bottom(scoped), detect_double_top(scoped)):
        if hit is not None:
            hits.append(hit.to_dict())
    return hits


def find_pivots(df: pl.DataFrame, window: int = PIVOT_WINDOW) -> list[dict]:
    rows = df.sort("date").select("date", "high", "low").to_dicts()
    radius = max(1, window // 2)
    pivots = []
    for i in range(radius, len(rows) - radius):
        segment = rows[i - radius:i + radius + 1]
        center = rows[i]
        highs = [_num(x["high"]) for x in segment]
        lows = [_num(x["low"]) for x in segment]
        if any(v is None for v in highs + lows):
            continue
        high = _num(center["high"])
        low = _num(center["low"])
        if high == max(highs) and highs.count(high) == 1:
            pivots.append({"type": "high", "date": _date(center), "price": high, "strength": _strength(high, highs)})
        if low == min(lows) and lows.count(low) == 1:
            pivots.append({"type": "low", "date": _date(center), "price": low, "strength": _strength(low, lows)})
    return pivots


def detect_breakout(df: pl.DataFrame, lookback: int = 60) -> PatternHit | None:
    rows = df.sort("date").tail(lookback + 1).to_dicts()
    if len(rows) < lookback + 1:
        return None
    last = rows[-1]
    prev_highs = [_num(r["high"]) for r in rows[:-1]]
    close = _num(last["close"])
    if close is None or any(v is None for v in prev_highs):
        return None
    resistance = max(prev_highs)
    if close <= resistance:
        return None
    volumes = [_num(r.get("volume")) for r in rows[:-1]]
    volume = _num(last.get("volume"))
    avg_volume = sum(v for v in volumes if v is not None) / max(1, len([v for v in volumes if v is not None]))
    volume_ratio = volume / avg_volume if volume is not None and avg_volume > 0 else None
    confidence = 0.65 + (0.15 if volume_ratio and volume_ratio >= 1.2 else 0.0)
    return PatternHit("breakout", _date(last), min(confidence, 0.9), {"resistance": resistance, "close": close, "volume_ratio": volume_ratio})


def detect_consolidation(df: pl.DataFrame, lookback: int = 20, max_range_pct: float = 0.12) -> PatternHit | None:
    rows = df.sort("date").tail(lookback).to_dicts()
    if len(rows) < lookback:
        return None
    high = max(_num(r["high"]) for r in rows)
    low = min(_num(r["low"]) for r in rows)
    close = _num(rows[-1]["close"])
    if close is None or close <= 0:
        return None
    range_pct = (high - low) / close
    if range_pct > max_range_pct:
        return None
    confidence = 0.7 + min(0.15, (max_range_pct - range_pct) / max_range_pct * 0.15)
    return PatternHit("consolidation", _date(rows[-1]), confidence, {"range_pct": range_pct, "high": high, "low": low})


def detect_double_bottom(df: pl.DataFrame) -> PatternHit | None:
    pivots = [p for p in find_pivots(df) if p["type"] == "low"]
    rows = df.sort("date").to_dicts()
    if len(pivots) < 2:
        return None
    left, right = pivots[-2], pivots[-1]
    li = _index_for_date(rows, left["date"])
    ri = _index_for_date(rows, right["date"])
    if li is None or ri is None or ri - li < 5:
        return None
    if abs(right["price"] / left["price"] - 1) > 0.05:
        return None
    neckline = max(_num(r["high"]) for r in rows[li:ri + 1])
    rebound = neckline / min(left["price"], right["price"]) - 1
    if rebound < 0.08:
        return None
    last_close = _num(rows[-1]["close"])
    confidence = 0.62 + (0.18 if last_close and last_close > neckline else 0.0)
    return PatternHit("double_bottom", _date(rows[-1]), confidence, {"left_low": left["price"], "right_low": right["price"], "neckline": neckline})


def detect_double_top(df: pl.DataFrame) -> PatternHit | None:
    pivots = [p for p in find_pivots(df) if p["type"] == "high"]
    rows = df.sort("date").to_dicts()
    if len(pivots) < 2:
        return None
    left, right = pivots[-2], pivots[-1]
    li = _index_for_date(rows, left["date"])
    ri = _index_for_date(rows, right["date"])
    if li is None or ri is None or ri - li < 5:
        return None
    if abs(right["price"] / left["price"] - 1) > 0.05:
        return None
    neckline = min(_num(r["low"]) for r in rows[li:ri + 1])
    pullback = max(left["price"], right["price"]) / neckline - 1
    if pullback < 0.08:
        return None
    last_close = _num(rows[-1]["close"])
    confidence = 0.62 + (0.18 if last_close and last_close < neckline else 0.0)
    return PatternHit("double_top", _date(rows[-1]), confidence, {"left_high": left["price"], "right_high": right["price"], "neckline": neckline})


def _num(value) -> float | None:
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _date(row: dict) -> str:
    return str(row["date"])[:10]


def _strength(center: float, values: list[float]) -> float:
    avg = sum(values) / len(values)
    return abs(center / avg - 1.0) if avg else 0.0


def _index_for_date(rows: list[dict], ds: str) -> int | None:
    for i, row in enumerate(rows):
        if _date(row) == ds:
            return i
    return None
