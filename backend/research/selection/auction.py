"""Deterministic opening-auction overlay for the frozen daily candidate set."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AuctionOverlayConfig:
    """Fixed screen-grade rules; these thresholds are not an OOS-promoted strategy."""

    confirm_gap_min: float = -0.01
    confirm_gap_max: float = 0.03
    reject_gap_min: float = -0.02
    reject_gap_max: float = 0.05
    min_auction_amount: float = 1_000_000.0
    min_volume_ratio: float = 0.50
    max_volume_ratio: float = 5.0
    max_positions: int = 10
    max_per_industry: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _index_auction_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or row.get("symbol") or "").upper()
        if not symbol:
            continue
        if symbol in indexed:
            raise ValueError(f"duplicate auction symbol: {symbol}")
        indexed[symbol] = row
    return indexed


def _classify(
    *,
    gap: float | None,
    amount: float | None,
    volume_ratio: float | None,
    config: AuctionOverlayConfig,
) -> tuple[str, list[str]]:
    if gap is None or amount is None or volume_ratio is None:
        return "REJECT_MISSING", ["集合竞价价格、成交额或量比缺失"]
    if gap > config.reject_gap_max:
        return "REJECT_CHASE", [f"竞价高开{gap:.2%}，超过追高上限{config.reject_gap_max:.2%}"]
    if gap < config.reject_gap_min:
        return "REJECT_WEAK", [f"竞价低开{gap:.2%}，低于弱势下限{config.reject_gap_min:.2%}"]
    if volume_ratio > config.max_volume_ratio:
        return "REJECT_OVERHEAT", [f"竞价量比{volume_ratio:.2f}超过异常上限{config.max_volume_ratio:.2f}"]

    reasons: list[str] = []
    if not config.confirm_gap_min <= gap <= config.confirm_gap_max:
        reasons.append(
            f"竞价涨跌幅{gap:.2%}不在确认区间"
            f"[{config.confirm_gap_min:.2%}, {config.confirm_gap_max:.2%}]"
        )
    if amount < config.min_auction_amount:
        reasons.append(f"竞价成交额{amount:,.0f}低于{config.min_auction_amount:,.0f}")
    if volume_ratio < config.min_volume_ratio:
        reasons.append(f"竞价量比{volume_ratio:.2f}低于{config.min_volume_ratio:.2f}")
    if reasons:
        return "WATCH", reasons
    return "CONFIRMED", ["昨收质量信号有效，竞价价格、成交额和量比通过固定确认规则"]


def apply_auction_overlay(
    base_candidates: list[dict[str, Any]],
    auction_rows: list[dict[str, Any]],
    *,
    config: AuctionOverlayConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Overlay auction facts without changing the frozen previous-close base ranking."""
    config = config or AuctionOverlayConfig()
    if config.max_positions <= 0 or config.max_per_industry <= 0:
        raise ValueError("portfolio limits must be positive")

    auction_by_symbol = _index_auction_rows(auction_rows)
    ordered = sorted(
        base_candidates,
        key=lambda row: (int(row.get("eligible_rank") or 10**9), str(row.get("symbol") or "")),
    )
    output: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    industry_counts: Counter[str] = Counter()
    selected_symbols: list[str] = []

    for base in ordered:
        symbol = str(base.get("symbol") or "").upper()
        auction = auction_by_symbol.get(symbol, {})
        price = _number(auction.get("price"))
        pre_close = _number(auction.get("pre_close"))
        amount = _number(auction.get("amount"))
        volume_ratio = _number(auction.get("volume_ratio"))
        gap = price / pre_close - 1.0 if price is not None and pre_close not in (None, 0.0) else None
        status, reasons = _classify(
            gap=gap,
            amount=amount,
            volume_ratio=volume_ratio,
            config=config,
        )
        status_counts[status] += 1

        industry = str(base.get("industry_current") or "")
        portfolio_selected = False
        portfolio_reason = None
        if status == "CONFIRMED":
            if len(selected_symbols) >= config.max_positions:
                portfolio_reason = "早盘候选名额已满"
            elif industry and industry_counts[industry] >= config.max_per_industry:
                portfolio_reason = f"行业集中度限制：{industry}已达{config.max_per_industry}只"
            else:
                portfolio_selected = True
                selected_symbols.append(symbol)
                if industry:
                    industry_counts[industry] += 1

        output.append(
            {
                "symbol": symbol,
                "name": base.get("name"),
                "industry_current": base.get("industry_current"),
                "base_signal_date": base.get("signal_date"),
                "base_rank": int(base.get("eligible_rank") or 0),
                "base_score": _number(base.get("score")),
                "auction_trade_date": auction.get("trade_date"),
                "auction_price": price,
                "pre_close": pre_close,
                "auction_gap": gap,
                "auction_volume": _number(auction.get("vol")),
                "auction_amount": amount,
                "auction_turnover_rate": _number(auction.get("turnover_rate")),
                "auction_volume_ratio": volume_ratio,
                "auction_status": status,
                "auction_reasons": reasons,
                "portfolio_selected": portfolio_selected,
                "portfolio_reason": portfolio_reason,
            }
        )

    summary = {
        "base_candidate_count": len(ordered),
        "auction_matched_count": sum(row["auction_trade_date"] is not None for row in output),
        "status_counts": dict(sorted(status_counts.items())),
        "selected_count": len(selected_symbols),
        "selected_symbols": selected_symbols,
        "industry_counts": dict(sorted(industry_counts.items())),
    }
    return output, summary
