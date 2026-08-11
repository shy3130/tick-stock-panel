"""Fetch today's opening auction and overlay it on the frozen previous-close candidates."""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from research.paths import DATA_DIR, SELECTION_ARTIFACTS_DIR, ensure_artifact_dirs
from research.selection.auction import AuctionOverlayConfig, apply_auction_overlay
from research.selection.auction_data import fetch_auction_snapshot


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_JSON = SELECTION_ARTIFACTS_DIR / "selection_logic_v1.json"
BASE_CSV = SELECTION_ARTIFACTS_DIR / "selection_logic_v1_latest_audit.csv"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else []
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            serializable = dict(row)
            serializable["auction_reasons"] = "；".join(row["auction_reasons"])
            writer.writerow(serializable)
        temporary = Path(handle.name)
    temporary.replace(path)


def _base_candidates() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not BASE_JSON.exists() or not BASE_CSV.exists():
        raise FileNotFoundError("run research.selection.run_selection_logic_v1 first")
    payload = json.loads(BASE_JSON.read_text(encoding="utf-8"))
    frame = (
        pl.read_csv(BASE_CSV)
        .filter(pl.col("decision") != "rejected_signal")
        .with_columns(
            pl.col("eligible_rank").cast(pl.Int64),
            pl.col("score").cast(pl.Float64),
        )
    )
    rows = frame.to_dicts()
    if any("ST" in str(row.get("name") or "").upper() for row in rows):
        raise ValueError("base candidates unexpectedly contain ST names")
    return payload, rows


def run(*, trading_day: date, token: str, api_base: str) -> dict[str, Any]:
    now = datetime.now(ASIA_SHANGHAI)
    if trading_day == now.date() and now.time() < time(9, 26):
        raise ValueError("today's opening auction is not final before 09:26 Asia/Shanghai")

    ensure_artifact_dirs()
    base_payload, base_rows = _base_candidates()
    base_signal_date = date.fromisoformat(base_payload["latest_audit"]["signal_date"])
    if base_signal_date >= trading_day:
        raise ValueError("base signal date must precede the auction trading date")

    auction, raw_path = fetch_auction_snapshot(
        trading_day=trading_day,
        token=token,
        api_base=api_base,
    )

    config = AuctionOverlayConfig()
    rows, summary = apply_auction_overlay(base_rows, auction.to_dicts(), config=config)
    stamp = trading_day.strftime("%Y%m%d")
    json_path = SELECTION_ARTIFACTS_DIR / f"auction_selection_{stamp}.json"
    csv_path = SELECTION_ARTIFACTS_DIR / f"auction_selection_{stamp}.csv"
    payload = {
        "status": "LIVE_SCREEN_ONLY",
        "as_of": now.isoformat(),
        "auction_trade_date": trading_day.isoformat(),
        "base_signal_date": base_signal_date.isoformat(),
        "source": {
            "provider": "Tushare",
            "api": "stk_auction",
            "raw_snapshot": raw_path.relative_to(DATA_DIR.parent).as_posix(),
            "document": "https://tushare.pro/document/2?doc_id=369",
        },
        "rules": config.to_dict(),
        "ranking": "previous-close quality score only; auction confirms or rejects but is not fitted as a score",
        "summary": summary,
        "selected": [row for row in rows if row["portfolio_selected"]],
        "candidates": rows,
        "limitations": [
            "Auction overlay thresholds are fixed screen-grade heuristics and have not passed historical OOS validation.",
            "This is an idea-candidate list, not a final trade recommendation or guaranteed executable price.",
            "Current industry labels are not point-in-time and are used only for same-day diversification.",
        ],
    }
    _atomic_json(json_path, payload)
    _atomic_csv(csv_path, rows)
    print(f"[auction-selection-v1] {summary['selected_count']} selected -> {json_path}", flush=True)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Opening-auction overlay for quality candidates")
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"), help="YYYYMMDD")
    parser.add_argument("--ts-token", default="", help="prefer TUSHARE_TOKEN environment variable")
    parser.add_argument("--api-base", default="", help="prefer TUSHARE_API_BASE environment variable")
    args = parser.parse_args()
    token = args.ts_token or os.getenv("TUSHARE_TOKEN", "")
    api_base = args.api_base or os.getenv("TUSHARE_API_BASE", "http://api.tushare.pro")
    run(trading_day=datetime.strptime(args.date, "%Y%m%d").date(), token=token, api_base=api_base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
