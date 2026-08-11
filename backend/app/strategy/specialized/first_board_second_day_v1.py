"""一进二竞价策略 v1 的确定性筛选与评分规则。

该策略需要 9:25 集合竞价成交额，不能由标准日线策略引擎伪装执行。研究 runner 负责
准备前一交易日首板与均线字段，本模块只做无 I/O 的规则判断和评分。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


STRATEGY_META = {
    "id": "first_board_second_day_v1",
    "name": "一进二竞价 v1（实验）",
    "lifecycle": "experimental",
    "evidence_status": "live_screen_only_not_historical_oos",
    "execution_backend": "specialized_auction_runner",
}


@dataclass(frozen=True, slots=True)
class FirstBoardSecondDayConfig:
    ratio_min: float = 0.08
    ratio_ideal_max: float = 0.12
    ratio_max: float = 0.20
    gap_min: float = 0.06
    gap_max: float = 0.08
    ratio_points: float = 30.0
    gap_points: float = 30.0
    ma_points: float = 20.0
    cross_points: float = 20.0
    cross_min_count: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _auction_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or row.get("symbol") or "").upper()
        if not symbol:
            continue
        if symbol in indexed:
            raise ValueError(f"duplicate auction symbol: {symbol}")
        indexed[symbol] = row
    return indexed


def _ratio_score(ratio: float | None, config: FirstBoardSecondDayConfig) -> float:
    if ratio is None or ratio < config.ratio_min or ratio > config.ratio_max:
        return 0.0
    if ratio <= config.ratio_ideal_max:
        return config.ratio_points
    width = config.ratio_max - config.ratio_ideal_max
    return config.ratio_points * max(0.0, config.ratio_max - ratio) / width


def evaluate_first_board_candidates(
    candidates: list[dict[str, Any]],
    auction_rows: list[dict[str, Any]],
    *,
    config: FirstBoardSecondDayConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evaluate an audited first-board pool against one 09:25 auction snapshot."""
    config = config or FirstBoardSecondDayConfig()
    if not (
        0 <= config.ratio_min <= config.ratio_ideal_max < config.ratio_max
        and 0 <= config.gap_min < config.gap_max
        and 1 <= config.cross_min_count <= 4
    ):
        raise ValueError("invalid first-board second-day configuration")

    auction_by_symbol = _auction_index(auction_rows)
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol:
            raise ValueError("candidate symbol is required")
        if symbol in seen:
            raise ValueError(f"duplicate candidate symbol: {symbol}")
        seen.add(symbol)
        auction = auction_by_symbol.get(symbol, {})

        first_board_amount = _finite(candidate.get("first_board_amount"))
        auction_amount = _finite(auction.get("amount"))
        auction_price = _finite(auction.get("price"))
        pre_close = _finite(auction.get("pre_close"))
        ratio = (
            auction_amount / first_board_amount
            if auction_amount is not None and first_board_amount not in (None, 0.0)
            else None
        )
        gap = (
            auction_price / pre_close - 1.0
            if auction_price is not None and pre_close not in (None, 0.0)
            else None
        )

        ma_values = {name: _finite(candidate.get(name)) for name in ("ma5", "ma10", "ma20", "ma60")}
        previous_mas = {
            name: _finite(candidate.get(f"{name}_previous"))
            for name in ("ma5", "ma10", "ma20")
        }
        ma_available = all(value is not None for value in (*ma_values.values(), *previous_mas.values()))
        ma_bullish = bool(
            ma_available
            and ma_values["ma5"] > ma_values["ma10"] > ma_values["ma20"]
            and all(ma_values[name] > previous_mas[name] for name in ("ma5", "ma10", "ma20"))
        )

        first_board_open = _finite(candidate.get("first_board_open"))
        first_board_close = _finite(candidate.get("first_board_close"))
        crossed_mas: list[str] = []
        if (
            first_board_open is not None
            and first_board_close is not None
            and first_board_close > first_board_open
        ):
            crossed_mas = [
                name.upper()
                for name, value in ma_values.items()
                if value is not None and first_board_open <= value < first_board_close
            ]
        cross_count = len(crossed_mas)

        failures: list[str] = []
        if int(candidate.get("consecutive_limit_ups") or 0) != 1:
            failures.append("前一交易日不是首板，或已经属于连板")
        if bool(candidate.get("is_st")):
            failures.append("ST、*ST或退市风险股票已排除")
        if not bool(candidate.get("is_listed", True)):
            failures.append("当前不是正常上市状态")
        if not auction:
            failures.append("缺少9:25集合竞价记录")
        if ratio is None:
            failures.append("竞价成交额或首板日全天成交额缺失")
        elif ratio < config.ratio_min:
            failures.append(f"竞价成交额占比{ratio:.2%}低于{config.ratio_min:.0%}")
        elif ratio > config.ratio_max:
            failures.append(f"竞价成交额占比{ratio:.2%}超过淘汰上限{config.ratio_max:.0%}")
        if gap is None:
            failures.append("竞价价格或前收盘价缺失")
        elif not config.gap_min <= gap <= config.gap_max:
            failures.append(
                f"竞价涨幅{gap:.2%}不在[{config.gap_min:.0%}, {config.gap_max:.0%}]"
            )
        if not ma_available:
            failures.append("MA5/10/20/60或前一日均线数据不足")
        elif not ma_bullish:
            failures.append("未满足MA5>MA10>MA20且三线同步向上")

        ratio_score = _ratio_score(ratio, config)
        gap_score = config.gap_points if gap is not None and config.gap_min <= gap <= config.gap_max else 0.0
        ma_score = config.ma_points if ma_bullish else 0.0
        cross_score = config.cross_points if cross_count >= config.cross_min_count else 0.0
        total_score = ratio_score + gap_score + ma_score + cross_score
        output.append(
            {
                "symbol": symbol,
                "name": candidate.get("name"),
                "first_board_date": candidate.get("first_board_date"),
                "consecutive_limit_ups": int(candidate.get("consecutive_limit_ups") or 0),
                "first_board_open": first_board_open,
                "first_board_close": first_board_close,
                "first_board_amount": first_board_amount,
                "auction_trade_date": auction.get("trade_date"),
                "auction_price": auction_price,
                "pre_close": pre_close,
                "auction_amount": auction_amount,
                "auction_amount_ratio": ratio,
                "auction_gap": gap,
                "ma5": ma_values["ma5"],
                "ma10": ma_values["ma10"],
                "ma20": ma_values["ma20"],
                "ma60": ma_values["ma60"],
                "ma_bullish_rising": ma_bullish,
                "crossed_mas": crossed_mas,
                "cross_count": cross_count,
                "score_components": {
                    "auction_amount_ratio": round(ratio_score, 6),
                    "auction_gap": round(gap_score, 6),
                    "ma_bullish_rising": round(ma_score, 6),
                    "single_candle_cross": round(cross_score, 6),
                },
                "score": round(total_score, 6),
                "decision": "SELECTED" if not failures else "REJECTED",
                "failure_reasons": failures,
            }
        )

    output.sort(
        key=lambda row: (
            row["decision"] != "SELECTED",
            -float(row["score"]),
            abs(float(row["auction_amount_ratio"] or 0.0) - 0.10),
            abs(float(row["auction_gap"] or 0.0) - 0.07),
            row["symbol"],
        )
    )
    selected = [row for row in output if row["decision"] == "SELECTED"]
    for rank, row in enumerate(selected, start=1):
        row["rank"] = rank
    summary = {
        "first_board_pool_count": len(output),
        "auction_matched_count": sum(row["auction_trade_date"] is not None for row in output),
        "selected_count": len(selected),
        "selected_symbols": [row["symbol"] for row in selected],
        "rejected_count": len(output) - len(selected),
    }
    return output, summary
