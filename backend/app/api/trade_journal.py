"""Trade Journal API."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.config import settings
from app.services.trade_journal import store
from app.services.trade_journal.benchmark import account_excess, per_trip_excess
from app.services.trade_journal.diagnose import diagnose
from app.services.trade_journal.fifo import pair_roundtrips
from app.services.trade_journal.models import CashEvent, Fill, LedgerSummary, Roundtrip
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
    append: Annotated[bool, Form()] = False,
    sheet: Annotated[str | None, Form()] = None,
    mapping: Annotated[str | None, Form()] = None,
    benchmark: Annotated[str, Form()] = "000300.SH",
    account_id: Annotated[str, Form()] = "default",
    narrative: Annotated[bool, Form()] = False,
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

    account_id = _clean_account_id(account_id)
    picked_mapping = _parse_mapping(mapping) or guessed or THS_PRESET["mapping"]
    new_fills, new_events, warnings = normalize_rows(df, picked_mapping, account_id=account_id)
    if not new_fills:
        raise HTTPException(status_code=400, detail="没有解析到买入/卖出成交")

    source, fills, events, deduped_fills, deduped_events = _merge_source(
        new_fills,
        new_events,
        append=append,
        import_meta={
            "file_name": file.filename or "",
            "account_id": account_id,
            "imported_at": datetime.now(UTC).isoformat(),
            "sha256": sha256(data).hexdigest(),
        },
    )
    benchmark = _normalize_benchmark(benchmark)
    start = min(f.date for f in fills)
    end = max(f.date for f in fills)
    repo = getattr(request.app.state, "repo", None)
    trading_days, index_closes = _market_context(repo, benchmark, start, end)
    trips, open_positions, pair_warnings = pair_roundtrips(fills, events, trading_days)
    warnings.extend(pair_warnings)
    price_lookup, uncovered_symbols = build_price_lookup(fills, settings.data_dir)
    if uncovered_symbols:
        warnings.append(f"追涨诊断: {len(uncovered_symbols)} 只标的无本地日K或历史不足20日未覆盖")

    summary = _summary(trips, open_positions)
    payload = {
        "imported_at": datetime.now(UTC).isoformat(),
        "accounts": _accounts(fills),
        "import": {
            "mode": "append" if append else "replace",
            "account_id": account_id,
            "new_fills": len(new_fills),
            "deduped_fills": deduped_fills,
            "deduped_events": deduped_events,
        },
        "trips": [asdict(t) | _roundtrip_metrics(t) for t in trips],
        "summary": asdict(summary),
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
    if narrative:
        payload["narrative"] = _narrative(summary, payload["diagnosis"], payload["benchmark"]["account"])
    store.write_source(settings.data_dir, source)
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


def _clean_account_id(raw: str) -> str:
    value = str(raw or "").strip()
    return value[:64] or "default"


def _merge_source(
    new_fills: list[Fill],
    new_events: list[CashEvent],
    append: bool,
    import_meta: dict,
) -> tuple[dict, list[Fill], list[CashEvent], int, int]:
    old = store.read_source(settings.data_dir) if append else None
    old_fills = [Fill(**row) for row in (old or {}).get("fills", [])]
    old_events = [CashEvent(**row) for row in (old or {}).get("events", [])]
    fills, deduped_fills = _dedupe(old_fills + new_fills, _fill_key)
    events, deduped_events = _dedupe(old_events + new_events, _event_key)
    source = {
        "imports": [*((old or {}).get("imports", [])), import_meta],
        "fills": [asdict(fill) for fill in fills],
        "events": [asdict(event) for event in events],
    }
    return source, fills, events, deduped_fills, deduped_events


def _dedupe(items: list, key_fn) -> tuple[list, int]:
    seen = set()
    out = []
    dupes = 0
    for item in items:
        key = key_fn(item)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        out.append(item)
    return out, dupes


def _fill_key(fill: Fill) -> tuple:
    return (
        fill.account_id,
        fill.date,
        fill.time,
        fill.symbol,
        fill.side,
        round(fill.qty, 8),
        round(fill.price, 8),
        round(fill.amount, 8),
        round(fill.fee, 8),
    )


def _event_key(event: CashEvent) -> tuple:
    return (event.account_id, event.date, event.symbol, event.kind, round(event.amount, 8))


def _accounts(fills: list[Fill]) -> list[dict]:
    counts: dict[str, int] = {}
    for fill in fills:
        counts[fill.account_id] = counts.get(fill.account_id, 0) + 1
    return [{"id": account_id, "fills": counts[account_id]} for account_id in sorted(counts)]


def _narrative(summary: LedgerSummary, diagnosis: dict, benchmark: dict) -> str:
    flags = [name for name, item in diagnosis.items() if isinstance(item, dict) and item.get("flag")]
    excess = benchmark.get("excess")
    excess_text = "暂无基准超额" if excess is None else f"基准超额 {excess:.1%}"
    flag_text = ", 需重点复盘: " + "、".join(flags) if flags else ", 未触发主要行为风险标签"
    return (
        f"本次合并台账共 {summary.total_trips} 个完成回合, "
        f"胜率 {summary.win_rate:.1%}, 总盈亏 {summary.total_pnl:.2f}, {excess_text}{flag_text}。"
    )


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


def _normalize_benchmark(symbol: str) -> str:
    allowed = {item["symbol"] for item in BENCHMARKS}
    return symbol if symbol in allowed else "000300.SH"
