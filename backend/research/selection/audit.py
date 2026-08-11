"""Convert executable quality-score matrices into human-readable decision rows."""
from __future__ import annotations

import math
from collections import Counter
from typing import Any


CHECK_LABELS = {
    "finite_history": "历史数据不足或指标非有限值",
    "ma_alignment": "未满足 MA5>MA10>MA20>MA60",
    "close_above_ma20": "收盘价低于MA20",
    "momentum_20d_floor": "20日动量低于门槛",
    "momentum_20d_ceiling": "20日涨幅过热",
    "momentum_60d_floor": "60日趋势强度不足",
    "ma20_bias_ceiling": "偏离MA20过远，追高风险",
    "annual_vol_ceiling": "20日年化波动过高",
    "gap_ceiling": "当日跳空幅度过大",
    "liquidity_floor": "20日平均成交额不足",
}


def _number(value: Any, digits: int = 6) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(parsed, digits) if math.isfinite(parsed) else None


def build_decision_rows(
    *,
    market,
    result: dict[str, Any],
    time_id: int,
    industries: dict[str, str] | None = None,
    max_positions: int = 10,
    max_per_industry: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if max_positions <= 0 or max_per_industry <= 0:
        raise ValueError("portfolio limits must be positive")
    industries = industries or {}
    eligible_ids = [
        asset_id
        for asset_id in range(len(market.symbols))
        if bool(result["eligible"][time_id, asset_id])
    ]
    eligible_ids.sort(
        key=lambda asset_id: (-float(result["score"][time_id, asset_id]), market.symbols[asset_id])
    )

    selected: set[int] = set()
    industry_counts: Counter[str] = Counter()
    portfolio_rejections: dict[int, str] = {}
    for asset_id in eligible_ids:
        symbol = market.symbols[asset_id]
        industry = industries.get(symbol)
        industry_label = industry or "未知行业"
        if len(selected) >= max_positions:
            portfolio_rejections[asset_id] = "组合持仓名额已满"
            continue
        if industry and industry_counts[industry] >= max_per_industry:
            portfolio_rejections[asset_id] = (
                f"行业集中度限制：{industry_label}已达{max_per_industry}只"
            )
            continue
        selected.add(asset_id)
        if industry:
            industry_counts[industry] += 1

    rank_by_id = {asset_id: rank for rank, asset_id in enumerate(eligible_ids, start=1)}
    rows: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    for asset_id, symbol in enumerate(market.symbols):
        failed_checks = [
            CHECK_LABELS[check]
            for check, mask in result["checks"].items()
            if not bool(mask[time_id, asset_id])
        ]
        if failed_checks:
            decision = "rejected_signal"
            reasons = failed_checks
        elif asset_id not in selected:
            decision = "rejected_portfolio"
            reasons = [portfolio_rejections[asset_id]]
        else:
            decision = "selected_signal"
            reasons = [
                "趋势排列通过",
                "动量处于非过热区间",
                "量价与流动性通过",
                "风险扣分后综合排名入围",
            ]
        if decision != "selected_signal":
            for reason in reasons:
                rejection_counts[reason] += 1

        row = {
            "signal_date": market.timestamp_labels[time_id],
            "symbol": symbol,
            "name": market.names[asset_id],
            "industry_current": industries.get(symbol),
            "decision": decision,
            "eligible_rank": rank_by_id.get(asset_id),
            "score": _number(result["score"][time_id, asset_id], 4),
            "reasons": reasons,
        }
        for group_name in ("features", "components"):
            for name, values in result[group_name].items():
                row[name] = _number(values[time_id, asset_id])
        rows.append(row)

    summary = {
        "universe_size": len(rows),
        "signal_eligible": len(eligible_ids),
        "selected_signal_count": len(selected),
        "signal_rejected_count": sum(row["decision"] == "rejected_signal" for row in rows),
        "portfolio_rejected_count": sum(row["decision"] == "rejected_portfolio" for row in rows),
        "selected_symbols": [
            market.symbols[asset_id]
            for asset_id in eligible_ids
            if asset_id in selected
        ],
        "rejection_counts": dict(rejection_counts.most_common()),
        "industry_counts": dict(sorted(industry_counts.items())),
    }
    return rows, summary
