"""Trade Journal API."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.config import settings
from app.services.trade_journal import store
from app.services.trade_journal.benchmark import account_excess, per_trip_excess
from app.services.trade_journal.diagnose import diagnose
from app.services.trade_journal.fifo import pair_roundtrips
from app.services.trade_journal.models import LedgerSummary, Roundtrip
from app.services.trade_journal.parser import normalize_rows, read_upload
from app.services.trade_journal.presets import PRESETS, THS_PRESET, guess_mapping
from app.services.trade_journal.pricepos import build_price_lookup

router = APIRouter(prefix="/api/journal", tags=["trade-journal"])

BENCHMARKS = [
    {"symbol": "000300.SH", "name": "沪深300"},
    {"symbol": "000905.SH", "name": "中证500"},
    {"symbol": "399006.SZ", "name": "创业板指"},
    {"symbol": "000688.SH", "name": "科创50"},
]


@router.get("/presets")
def presets():
    return {"presets": PRESETS, "benchmarks": BENCHMARKS}


@router.post("/upload")
async def upload_journal(
    request: Request,
    file: Annotated[UploadFile, File()],
    commit: bool = False,
    sheet: Annotated[str | None, Form()] = None,
    mapping: Annotated[str | None, Form()] = None,
    benchmark: Annotated[str, Form()] = "000300.SH",
):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")

    sheets, df = read_upload(data, file.filename or "", sheet)
    guessed = guess_mapping(df.columns)
    if not commit:
        return {
            "sheets": sheets,
            "columns": df.columns,
            "guessed_mapping": guessed,
            "preview_rows": df.head(20).to_dicts(),
            "row_count": df.height,
            "warnings": [],
        }

    picked_mapping = _parse_mapping(mapping) or guessed or THS_PRESET["mapping"]
    fills, events, warnings = normalize_rows(df, picked_mapping)
    if not fills:
        raise HTTPException(status_code=400, detail="没有解析到买入/卖出成交")

    start = min(f.date for f in fills)
    end = max(f.date for f in fills)
    repo = getattr(request.app.state, "repo", None)
    trading_days, index_closes = _market_context(repo, benchmark, start, end)
    trips, open_positions, pair_warnings = pair_roundtrips(fills, events, trading_days)
    warnings.extend(pair_warnings)
    price_lookup = build_price_lookup(fills, settings.data_dir)

    payload = {
        "imported_at": datetime.now(UTC).isoformat(),
        "trips": [asdict(t) | _roundtrip_metrics(t) for t in trips],
        "summary": asdict(_summary(trips, open_positions)),
        "diagnosis": diagnose(trips, fills, price_lookup),
        "benchmark": {
            "code": benchmark,
            "name": _benchmark_name(benchmark),
            "account": account_excess(trips, index_closes),
            "per_trip": per_trip_excess(trips, index_closes),
            "noise_note": "基准超额仅用于方向性复盘, 未按账户现金流精确加权。",
        },
        "warnings": warnings,
    }
    store.write_ledger(settings.data_dir, payload)
    return payload


@router.get("/ledger")
def get_ledger():
    ledger = store.read_ledger(settings.data_dir)
    if ledger is None:
        raise HTTPException(status_code=404, detail="尚未导入交易复盘台账")
    return ledger


@router.delete("/ledger")
def delete_ledger():
    return {"deleted": store.delete_ledger(settings.data_dir)}


def _parse_mapping(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="mapping 不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="mapping 必须是对象")
    return {str(k): str(v) for k, v in value.items()}


def _market_context(repo, benchmark: str, start: str, end: str) -> tuple[list[str] | None, dict[str, float]]:
    if repo is None:
        return None, {}
    try:
        start_d = date.fromisoformat(start) - timedelta(days=30)
        end_d = date.fromisoformat(end) + timedelta(days=1)
        df = repo.get_index_daily(benchmark, start_d, end_d, columns=["date", "close"])
    except Exception:
        return None, {}
    if df is None or df.is_empty():
        return None, {}
    rows = df.select(["date", "close"]).to_dicts()
    days = sorted(str(r["date"])[:10] for r in rows)
    closes = {str(r["date"])[:10]: float(r["close"]) for r in rows if r.get("close") is not None}
    return days, closes


def _summary(trips: list[Roundtrip], open_positions: list[dict]) -> LedgerSummary:
    pnls = [t.total_pnl for t in trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return LedgerSummary(
        total_trips=len(trips),
        win_trips=len(wins),
        total_pnl=sum(pnls),
        total_dividend=sum(t.dividend for t in trips),
        total_fees=sum(t.fees for t in trips),
        win_rate=len(wins) / len(trips) if trips else 0.0,
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        profit_factor=sum(wins) / abs(sum(losses)) if losses else 0.0,
        open_positions=open_positions,
    )


def _roundtrip_metrics(t: Roundtrip) -> dict:
    return {"pnl": t.pnl, "total_pnl": t.total_pnl, "pnl_pct": t.pnl_pct, "buy_avg": t.buy_avg, "sell_avg": t.sell_avg}


def _benchmark_name(symbol: str) -> str:
    for item in BENCHMARKS:
        if item["symbol"] == symbol:
            return item["name"]
    return symbol
