"""Run the specialized first-board-to-second-board 09:25 auction screen."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.strategy.specialized.first_board_second_day_v1 import (
    STRATEGY_META,
    FirstBoardSecondDayConfig,
    evaluate_first_board_candidates,
)
from research.paths import DATA_DIR, SELECTION_ARTIFACTS_DIR, ensure_artifact_dirs
from research.selection.auction_data import load_or_fetch_auction_snapshot


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RISK_NAME = re.compile(r"ST|\*ST|退", re.IGNORECASE)


def _partition_dates(dataset: Path) -> list[date]:
    dates: list[date] = []
    for path in dataset.glob("date=*"):
        try:
            dates.append(date.fromisoformat(path.name.removeprefix("date=")))
        except ValueError:
            continue
    return sorted(set(dates))


def _previous_trading_day(trading_day: date) -> date:
    previous = [day for day in _partition_dates(DATA_DIR / "kline_daily_enriched") if day < trading_day]
    if not previous:
        raise ValueError(f"no complete daily bar before {trading_day}")
    return previous[-1]


def _first_board_candidates(first_board_date: date) -> list[dict[str, Any]]:
    available = [
        day for day in _partition_dates(DATA_DIR / "kline_daily_enriched")
        if day <= first_board_date
    ]
    if not available or available[-1] != first_board_date:
        raise ValueError(f"missing enriched daily partition for {first_board_date}")
    warmup_dates = available[-80:]
    source = str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet")
    history = (
        pl.scan_parquet(source, hive_partitioning=True)
        .filter(pl.col("date").is_between(warmup_dates[0], first_board_date, closed="both"))
        .select(
            "symbol",
            "date",
            "open",
            "close",
            "amount",
            "consecutive_limit_ups",
        )
        .sort(["symbol", "date"])
        .with_columns(
            [
                pl.col("close")
                .rolling_mean(window_size=window, min_samples=window)
                .over("symbol")
                .alias(f"ma{window}")
                for window in (5, 10, 20, 60)
            ]
        )
        .with_columns(
            [
                pl.col(f"ma{window}").shift(1).over("symbol").alias(f"ma{window}_previous")
                for window in (5, 10, 20)
            ]
        )
        .filter(
            (pl.col("date") == first_board_date)
            & (pl.col("consecutive_limit_ups") == 1)
        )
        .rename(
            {
                "date": "first_board_date",
                "open": "first_board_open",
                "close": "first_board_close",
                "amount": "first_board_amount",
            }
        )
        .collect()
    )

    basic_path = DATA_DIR / "tushare_stock_basic" / "all.parquet"
    if not basic_path.is_file():
        raise FileNotFoundError(f"missing stock basic data: {basic_path}")
    basic = pl.read_parquet(basic_path).select(
        pl.col("ts_code").alias("symbol"),
        pl.col("name").fill_null(""),
        pl.col("list_status").fill_null(""),
    )
    joined = history.join(basic, on="symbol", how="left")
    rows = joined.to_dicts()
    for row in rows:
        row["first_board_date"] = str(row["first_board_date"])
        row["is_st"] = bool(_RISK_NAME.search(str(row.get("name") or "")))
        row["is_listed"] = row.get("list_status") == "L"
    return rows


def _raw_close_map(trading_day: date, symbols: list[str]) -> dict[str, float]:
    directory = DATA_DIR / "kline_daily" / f"date={trading_day.isoformat()}"
    files = list(directory.glob("*.parquet")) if directory.is_dir() else []
    if not files or not symbols:
        return {}
    frame = pl.concat([pl.read_parquet(path) for path in files], how="diagonal_relaxed")
    close_column = "raw_close" if "raw_close" in frame.columns else "close"
    return {
        str(symbol): float(close)
        for symbol, close in frame.filter(pl.col("symbol").is_in(symbols))
        .select("symbol", close_column)
        .iter_rows()
        if close is not None
    }


def _attach_feedback(rows: list[dict[str, Any]], trading_day: date) -> dict[str, Any]:
    selected = [row for row in rows if row["decision"] == "SELECTED"]
    symbols = [row["symbol"] for row in selected]
    day0_closes = _raw_close_map(trading_day, symbols)
    future_dates = [day for day in _partition_dates(DATA_DIR / "kline_daily") if day > trading_day]
    day1_date = future_dates[0] if future_dates else None
    day1_closes = _raw_close_map(day1_date, symbols) if day1_date else {}
    for row in selected:
        entry = row["auction_price"]
        day0_close = day0_closes.get(row["symbol"])
        day1_close = day1_closes.get(row["symbol"])
        row["feedback"] = {
            "entry_time": f"{trading_day.isoformat()} 09:25 Asia/Shanghai",
            "entry_price_assumption": entry,
            "fill_status": "assumed_for_research_not_verified",
            "same_day_close": day0_close,
            "same_day_return_gross": day0_close / entry - 1.0 if day0_close and entry else None,
            "next_trading_day": day1_date.isoformat() if day1_date else None,
            "next_day_close": day1_close,
            "next_day_return_gross": day1_close / entry - 1.0 if day1_close and entry else None,
        }
    return {
        "same_day_available": bool(selected) and len(day0_closes) == len(selected),
        "next_day_available": bool(selected) and len(day1_closes) == len(selected),
        "next_trading_day": day1_date.isoformat() if day1_date else None,
        "note": "gross mark-to-close feedback only; fees, slippage and actual 09:25 fills are not verified",
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "rank", "symbol", "name", "decision", "score", "first_board_date",
        "first_board_amount", "auction_trade_date", "auction_price", "auction_amount",
        "auction_amount_ratio", "auction_gap", "ma5", "ma10", "ma20", "ma60",
        "ma_bullish_rising", "cross_count", "crossed_mas", "score_components",
        "failure_reasons", "feedback",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            serializable = dict(row)
            serializable["crossed_mas"] = "/".join(row["crossed_mas"])
            serializable["failure_reasons"] = "；".join(row["failure_reasons"])
            serializable["score_components"] = json.dumps(row["score_components"], ensure_ascii=False)
            serializable["feedback"] = json.dumps(row.get("feedback"), ensure_ascii=False)
            writer.writerow(serializable)
        temporary = Path(handle.name)
    temporary.replace(path)


def run(
    *,
    trading_day: date,
    token: str,
    api_base: str,
    refresh_auction: bool = False,
) -> dict[str, Any]:
    ensure_artifact_dirs()
    first_board_date = _previous_trading_day(trading_day)
    candidates = _first_board_candidates(first_board_date)
    auction, raw_path, auction_source = load_or_fetch_auction_snapshot(
        trading_day=trading_day,
        token=token,
        api_base=api_base,
        refresh=refresh_auction,
    )
    config = FirstBoardSecondDayConfig()
    rows, summary = evaluate_first_board_candidates(
        candidates,
        auction.to_dicts(),
        config=config,
    )
    feedback = _attach_feedback(rows, trading_day)
    selected = [row for row in rows if row["decision"] == "SELECTED"]
    stamp = trading_day.strftime("%Y%m%d")
    json_path = SELECTION_ARTIFACTS_DIR / f"first_board_second_day_{stamp}.json"
    csv_path = SELECTION_ARTIFACTS_DIR / f"first_board_second_day_{stamp}.csv"
    payload = {
        "status": "LIVE_SCREEN_ONLY",
        "as_of": datetime.now(ASIA_SHANGHAI).isoformat(),
        "strategy": STRATEGY_META,
        "first_board_date": first_board_date.isoformat(),
        "auction_trade_date": trading_day.isoformat(),
        "universe": "previous complete trading-day consecutive_limit_ups == 1; ST/risk names rejected",
        "rules": config.to_dict(),
        "score_definition": {
            "auction_amount_ratio": "8%-12% = 30; 12%-20% linearly decays to 0; outside 8%-20% rejected",
            "auction_gap": "6%-8% = 30; outside rejected",
            "ma_bullish_rising": "MA5>MA10>MA20 and all three above previous-day values = 20; otherwise rejected",
            "single_candle_cross": "first-board bullish candle crosses at least 2 of MA5/10/20/60 = 20 bonus",
            "tie_break": "ratio distance to 10%, gap distance to 7%, then symbol",
        },
        "source": {
            "daily": "data/kline_daily_enriched",
            "auction": raw_path.relative_to(DATA_DIR.parent).as_posix(),
            "auction_load": auction_source,
        },
        "summary": summary,
        "feedback_status": feedback,
        "selected": selected,
        "candidates": rows,
        "limitations": [
            "Only locally archived auction dates can be replayed without fetching a separate historical auction dataset.",
            "The 09:25 price is an assumed research fill; queue position and actual execution are unknown.",
            "No threshold has passed fresh OOS validation; this strategy remains experimental and hidden from core defaults.",
        ],
    }
    _atomic_json(json_path, payload)
    _atomic_csv(csv_path, rows)
    print(f"[first-board-second-day-v1] {summary['selected_count']} selected -> {json_path}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="一进二 9:25 集合竞价实验筛选")
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"), help="YYYYMMDD")
    parser.add_argument("--refresh-auction", action="store_true")
    parser.add_argument("--ts-token", default="", help="prefer TUSHARE_TOKEN environment variable")
    parser.add_argument("--api-base", default="", help="prefer TUSHARE_API_BASE environment variable")
    args = parser.parse_args()
    run(
        trading_day=datetime.strptime(args.date, "%Y%m%d").date(),
        token=args.ts_token or os.getenv("TUSHARE_TOKEN", ""),
        api_base=args.api_base or os.getenv("TUSHARE_API_BASE", "http://api.tushare.pro"),
        refresh_auction=args.refresh_auction,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
