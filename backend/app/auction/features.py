"""从过程快照构建 as_of 安全的竞价特征。不含当日收盘/全日量比。"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

from app.auction.contracts import (
    AUCTION_FEATURE_VERSION,
    AuctionFinal,
    AuctionSnapshot,
    UnmatchedSide,
    hhmmss_from_ms,
    is_open_auction_point,
)
from app.auction.quality import quality_score


def build_latest_features(
    snapshots: list[AuctionSnapshot],
    *,
    as_of_ms: int,
    trade_date: date,
) -> list[dict]:
    """按标的聚合 as_of 之前已收到的开盘过程点。"""
    grouped: dict[str, list[AuctionSnapshot]] = defaultdict(list)
    for snap in snapshots:
        if snap.trade_date != trade_date:
            continue
        if snap.received_at_ms > as_of_ms:
            continue
        if not is_open_auction_point(hhmmss_from_ms(snap.source_time_ms)):
            continue
        grouped[snap.symbol].append(snap)

    rows: list[dict] = []
    for symbol, points in grouped.items():
        points.sort(key=lambda item: (item.source_time_ms, item.received_at_ms, item.sequence))
        rows.append(_feature_row(symbol, points, as_of_ms=as_of_ms, trade_date=trade_date))
    return rows


def _feature_row(
    symbol: str,
    points: list[AuctionSnapshot],
    *,
    as_of_ms: int,
    trade_date: date,
) -> dict:
    latest = points[-1]
    first = points[0]
    q_score, q_flags = quality_score(points, now_ms=as_of_ms)
    prices = [p.indicative_price for p in points if p.indicative_price is not None]
    matched = [p.matched_volume for p in points if p.matched_volume is not None]
    span_ms = latest.source_time_ms - first.source_time_ms
    minutes = span_ms / 60_000.0 if span_ms > 0 else None

    gap_pct = None
    if latest.indicative_price is not None and latest.pre_close not in (None, 0):
        gap_pct = latest.indicative_price / latest.pre_close - 1.0
    elif first.pre_close not in (None, 0) and latest.indicative_price is not None:
        gap_pct = latest.indicative_price / first.pre_close - 1.0

    matched_growth = None
    if len(matched) >= 2:
        matched_growth = matched[-1] - matched[0]
    matched_growth_rate = None
    if matched_growth is not None and minutes is not None:
        matched_growth_rate = matched_growth / minutes

    unmatched_match_ratio = None
    if latest.unmatched_volume is not None and latest.matched_volume not in (None, 0):
        unmatched_match_ratio = latest.unmatched_volume / latest.matched_volume

    slope = None
    if len(prices) >= 2 and prices[0] and minutes is not None:
        slope = (prices[-1] - prices[0]) / prices[0] * 10_000.0 / minutes

    stability = None
    drawdown = None
    if prices:
        peak = prices[0]
        max_dd = 0.0
        deviations = []
        mean = sum(prices) / len(prices)
        for price in prices:
            peak = max(peak, price)
            if peak:
                max_dd = max(max_dd, (peak - price) / peak)
            deviations.append(abs(price - mean))
        drawdown = max_dd * 10_000.0
        stability = (sum(deviations) / len(deviations)) / mean * 10_000.0 if mean else None

    switches = 0
    buy_ticks = 0
    known = 0
    last_side: UnmatchedSide | None = None
    for point in points:
        side = point.unmatched_side
        if side == UnmatchedSide.unknown:
            continue
        known += 1
        if side == UnmatchedSide.buy:
            buy_ticks += 1
        if (
            last_side is not None
            and side != last_side
            and side != UnmatchedSide.neutral
            and last_side != UnmatchedSide.neutral
        ):
            switches += 1
        last_side = side
    persistence = (buy_ticks / known) if known else None

    return {
        "feature_version": AUCTION_FEATURE_VERSION,
        "trade_date": trade_date.isoformat(),
        "symbol": symbol,
        "as_of_ms": as_of_ms,
        "available_at_ms": latest.received_at_ms,
        "source": latest.source,
        "point_count": len(points),
        "indicative_price": latest.indicative_price,
        "matched_volume": latest.matched_volume,
        "unmatched_volume": latest.unmatched_volume,
        "unmatched_side": str(latest.unmatched_side),
        "pre_close": latest.pre_close or first.pre_close,
        "gap_pct": gap_pct,
        "matched_growth": matched_growth,
        "matched_growth_rate_per_minute": matched_growth_rate,
        "unmatched_match_ratio": unmatched_match_ratio,
        "price_slope_bps_per_minute": slope,
        "price_stability_bps": stability,
        "max_drawdown_bps": drawdown,
        "buy_unmatched_persistence": persistence,
        "unmatched_direction_switches": switches,
        "quality_score": q_score,
        "quality_flags": q_flags,
        "log_matched": math.log1p(latest.matched_volume or 0.0),
        "log_growth": math.log1p(max(matched_growth or 0.0, 0.0)),
    }


def build_finals_only_features(
    finals: list[AuctionFinal],
    *,
    as_of_ms: int,
    trade_date: date,
) -> list[dict]:
    """无过程序列时用 09:25 撮合排行。不补匹配/未匹配, 质量分压低。"""
    rows: list[dict] = []
    for item in finals:
        if item.trade_date != trade_date or item.available_at_ms > as_of_ms:
            continue
        if item.open_price is None:
            continue
        gap = item.open_change_pct
        if gap is None and item.pre_close not in (None, 0):
            gap = item.open_price / item.pre_close - 1.0
        flags = list(item.quality_flags)
        if "finals_only" not in flags:
            flags.append("finals_only")
        rows.append(
            {
                "feature_version": AUCTION_FEATURE_VERSION,
                "trade_date": trade_date.isoformat(),
                "symbol": item.symbol,
                "as_of_ms": as_of_ms,
                "available_at_ms": item.available_at_ms,
                "source": item.source,
                "point_count": 0,
                "indicative_price": item.open_price,
                "matched_volume": item.open_volume,
                "unmatched_volume": None,
                "unmatched_side": "unknown",
                "pre_close": item.pre_close,
                "gap_pct": gap,
                "matched_growth": None,
                "matched_growth_rate_per_minute": None,
                "unmatched_match_ratio": None,
                "price_slope_bps_per_minute": None,
                "price_stability_bps": None,
                "max_drawdown_bps": None,
                "buy_unmatched_persistence": None,
                "unmatched_direction_switches": 0,
                "quality_score": 45.0,
                "quality_flags": flags,
                "log_matched": math.log1p(item.open_volume or 0.0),
                "log_growth": 0.0,
            }
        )
    return rows
