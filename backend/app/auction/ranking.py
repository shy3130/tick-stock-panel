"""风格透镜。只消费 features 行, 不回读日 K / enriched。"""

from __future__ import annotations

from collections.abc import Sequence

from app.auction.contracts import AuctionStyle


def parse_style(value: str | AuctionStyle | None) -> AuctionStyle:
    if isinstance(value, AuctionStyle):
        return value
    try:
        return AuctionStyle((value or "momentum").strip().lower())
    except ValueError:
        return AuctionStyle.momentum


def rank_features(
    rows: Sequence[dict],
    *,
    style: AuctionStyle | str = AuctionStyle.momentum,
    limit: int = 50,
) -> list[dict]:
    chosen = parse_style(style)
    scored = []
    for row in rows:
        raw = _raw_score(row, chosen)
        quality = float(row.get("quality_score") or 0.0)
        score = max(0.0, min(100.0, raw * (0.55 + 0.45 * quality / 100.0)))
        item = dict(row)
        item["style"] = str(chosen)
        item["score"] = round(score, 4)
        item["reasons"] = _reasons(row, chosen)
        scored.append(item)
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[: max(1, min(int(limit), 200))]


def _n(row: dict, key: str, default: float = 0.0) -> float:
    value = row.get(key)
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _raw_score(row: dict, style: AuctionStyle) -> float:
    gap = _n(row, "gap_pct") * 100.0
    persistence = _n(row, "buy_unmatched_persistence")
    unmatched_ratio = _n(row, "unmatched_match_ratio")
    log_matched = _n(row, "log_matched")
    log_growth = _n(row, "log_growth")
    slope = _n(row, "price_slope_bps_per_minute")
    stability = _n(row, "price_stability_bps")
    drawdown = _n(row, "max_drawdown_bps")
    switches = _n(row, "unmatched_direction_switches")

    if style == AuctionStyle.limit_up:
        return (
            30
            + 7 * gap
            + 18 * persistence
            + 7 * unmatched_ratio
            + 1.8 * log_matched
            - 2 * switches
        )
    if style == AuctionStyle.volume_price:
        return (
            36
            + 2.3 * log_matched
            + 3 * log_growth
            + 3 * gap
            + 0.035 * slope
            - 0.025 * stability
        )
    if style == AuctionStyle.swing:
        return (
            58
            + 2.2 * gap
            + 1.5 * log_growth
            - 0.055 * stability
            - 0.04 * drawdown
            - 1.5 * switches
        )
    return (
        42
        + 5 * gap
        + 0.09 * slope
        + 2.5 * log_growth
        + 12 * persistence
        - 0.025 * drawdown
    )


def _reasons(row: dict, style: AuctionStyle) -> list[str]:
    reasons: list[str] = []
    gap = _n(row, "gap_pct")
    if gap >= 0.03:
        reasons.append("高开")
    if style == AuctionStyle.limit_up:
        if _n(row, "buy_unmatched_persistence") >= 0.7:
            reasons.append("买盘持续")
        if _n(row, "unmatched_match_ratio") >= 0.3:
            reasons.append("未匹配厚")
    if style == AuctionStyle.volume_price and _n(row, "matched_growth") > 0:
        reasons.append("匹配量加速")
    if style == AuctionStyle.momentum and _n(row, "price_slope_bps_per_minute") > 0:
        reasons.append("价格上倾")
    if style == AuctionStyle.swing and _n(row, "price_stability_bps") < 40:
        reasons.append("路径平稳")
    flags = row.get("quality_flags") or []
    if "missing_unmatched" in flags:
        reasons.append("未匹配未知")
    return reasons
