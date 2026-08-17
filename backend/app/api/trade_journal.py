"""Trade Journal API."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from math import isfinite
from typing import Annotated

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

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
    {"symbol": "000300.INDEX", "name": "沪深300"},
    {"symbol": "000905.INDEX", "name": "中证500"},
    {"symbol": "399006.INDEX", "name": "创业板指"},
    {"symbol": "000688.INDEX", "name": "科创50"},
]
_MAX_FHOLD_NUMERIC_ABS = 1e100


class FholdJournalImportRequest(BaseModel):
    """Apply exactly the fhold transaction snapshot the user previewed."""

    snapshot_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    benchmark: str = "000300.INDEX"
    narrative: bool = False


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
    benchmark: Annotated[str, Form()] = "000300.INDEX",
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
    return _commit_journal(
        request,
        new_fills,
        new_events,
        append=append,
        import_meta={
            "file_name": file.filename or "",
            "account_id": account_id,
            "imported_at": datetime.now(UTC).isoformat(),
            "sha256": sha256(data).hexdigest(),
        },
        benchmark=benchmark,
        narrative=narrative,
        warnings=warnings,
    )


@router.get("/fhold-preview")
def preview_fhold_journal() -> dict:
    """Read fhold transactions without writing the local journal."""
    preview, _ = _read_fhold_journal_snapshot()
    return preview


@router.post("/fhold-import")
def import_fhold_journal(
    request: Request,
    payload: FholdJournalImportRequest,
) -> dict:
    """Append the exact fhold transaction snapshot the user previewed."""
    preview, fills = _read_fhold_journal_snapshot()
    if not preview["available"]:
        raise HTTPException(status_code=503, detail="fhold-cli 不可用或交易流水读取失败")
    snapshot_sha256 = preview["snapshot_sha256"]
    if not snapshot_sha256 or not compare_digest(payload.snapshot_sha256, snapshot_sha256):
        raise HTTPException(status_code=409, detail="fhold 流水已变化，请重新预览后再导入")
    if not fills:
        raise HTTPException(status_code=400, detail="fhold 没有可导入的成交")
    return _commit_journal(
        request,
        fills,
        [],
        append=True,
        import_meta={
            "source": "fhold",
            "account_id": "fhold",
            "imported_at": datetime.now(UTC).isoformat(),
            "snapshot_sha256": snapshot_sha256,
            "transaction_count": len(fills),
        },
        benchmark=payload.benchmark,
        narrative=payload.narrative,
        warnings=list(preview["warnings"]),
    )


@router.get("/ledger")
def get_ledger() -> dict | None:
    """返回已导入台账；尚未导入是正常空态，不制造 404 网络错误。"""
    ledger = store.read_ledger(settings.data_dir)
    if ledger is None:
        return None
    from app.services.skill_context import load_skill_context_safe

    methodology_context = load_skill_context_safe(
        "trade_journal", max_chars=4000, warnings=ledger.setdefault("warnings", [])
    )
    if methodology_context:
        ledger["methodology_context"] = methodology_context
    return ledger


@router.delete("/ledger")
def delete_ledger():
    return {"deleted": store.delete_ledger(settings.data_dir)}


@router.post("/feedback")
def save_feedback(payload: Annotated[dict, Body()]):
    rating = str(payload.get("rating") or "").strip()
    if rating not in {"helpful", "not_helpful"}:
        raise HTTPException(status_code=400, detail="rating 必须是 helpful 或 not_helpful")
    ledger = store.read_ledger(settings.data_dir) or {}
    store.append_feedback(
        settings.data_dir,
        {
            "rating": rating,
            "ledger_imported_at": ledger.get("imported_at"),
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    return {"ok": True}


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


def _commit_journal(
    request: Request,
    new_fills: list[Fill],
    new_events: list[CashEvent],
    *,
    append: bool,
    import_meta: dict,
    benchmark: str,
    narrative: bool,
    warnings: list[str],
) -> dict:
    if not new_fills:
        raise HTTPException(status_code=400, detail="没有解析到买入/卖出成交")
    # Lock read -> merge -> derived ledger -> two-file commit so concurrent appends retain facts.
    with store.journal_write_lock(settings.data_dir):
        return _commit_journal_locked(
            request,
            new_fills,
            new_events,
            append=append,
            import_meta=import_meta,
            benchmark=benchmark,
            narrative=narrative,
            warnings=warnings,
        )


def _commit_journal_locked(
    request: Request,
    new_fills: list[Fill],
    new_events: list[CashEvent],
    *,
    append: bool,
    import_meta: dict,
    benchmark: str,
    narrative: bool,
    warnings: list[str],
) -> dict:
    source, fills, events, deduped_fills, deduped_events, conflicting_fills = _merge_source(
        new_fills,
        new_events,
        append=append,
        import_meta=import_meta,
    )
    warnings = list(warnings)
    if conflicting_fills:
        warnings.append(
            f"fhold 有 {conflicting_fills} 条已导入交易发生变更; "
            "为保护既有 journal 事实, 未覆盖旧记录。"
        )
    benchmark = _normalize_benchmark(benchmark)
    start = min(fill.date for fill in fills)
    end = max(fill.date for fill in fills)
    repo = getattr(request.app.state, "repo", None)
    trading_days, index_closes = _market_context(repo, benchmark, start, end)
    trips, open_positions, pair_warnings = pair_roundtrips(fills, events, trading_days)
    warnings.extend(pair_warnings)
    price_lookup, uncovered_symbols = build_price_lookup(fills, settings.data_dir)
    if uncovered_symbols:
        warnings.append(f"追涨诊断: {len(uncovered_symbols)} 只标的无本地日K或历史不足20日未覆盖")

    summary = _summary(trips, open_positions)
    from app.services.skill_context import load_skill_context_safe

    methodology_context = load_skill_context_safe(
        "trade_journal", max_chars=4000, warnings=warnings
    )
    payload = {
        "imported_at": datetime.now(UTC).isoformat(),
        "accounts": _accounts(fills),
        "import": {
            "source": str(import_meta.get("source") or "upload"),
            "mode": "append" if append else "replace",
            "account_id": str(import_meta.get("account_id") or "default"),
            "new_fills": len(new_fills),
            "deduped_fills": deduped_fills,
            "deduped_events": deduped_events,
            "conflicting_fills": conflicting_fills,
        },
        "trips": [asdict(trip) | _roundtrip_metrics(trip) for trip in trips],
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
    if methodology_context:
        payload["methodology_context"] = methodology_context
    if narrative:
        payload["narrative"] = _narrative(
            summary, payload["diagnosis"], payload["benchmark"]["account"]
        )
    ledger = {key: value for key, value in payload.items() if key != "methodology_context"}
    try:
        store.write_journal(settings.data_dir, source, ledger)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="交易数据包含无法安全汇总的数值") from exc
    return payload


def _read_fhold_journal_snapshot() -> tuple[dict, list[Fill]]:
    """Normalize fhold's read-only transaction list into a previewable journal snapshot."""
    from app.services.trading import fhold_client

    result = fhold_client.fetch_transactions()
    if not result.get("available"):
        return {
            "available": False,
            "snapshot_sha256": None,
            "row_count": 0,
            "importable_count": 0,
            "skipped_count": 0,
            "accounts": [],
            "preview_rows": [],
            "warnings": ["fhold-cli 不可用或交易流水读取失败"],
        }, []

    skipped = Counter()
    fills: list[Fill] = []
    transactions = result.get("transactions") or []
    for transaction in transactions:
        fill, reason = _fhold_transaction_to_fill(transaction)
        if fill is None:
            skipped[reason or "字段无效"] += 1
            continue
        fills.append(fill)

    account_names = {
        str(account.get("id")): str(account.get("name") or "").strip()
        for account in result.get("accounts") or []
        if isinstance(account, dict) and account.get("id") is not None
    }
    counts = Counter(fill.account_id for fill in fills)
    accounts = [
        {
            "id": account_id,
            "name": account_names.get(account_id.removeprefix("fhold:"), ""),
            "fills": count,
        }
        for account_id, count in sorted(counts.items())
    ]
    warnings = [f"fhold 跳过 {count} 条: {reason}" for reason, count in sorted(skipped.items())]
    if not fills:
        warnings.append("fhold 没有可导入的买入/卖出成交")
    canonical = [asdict(fill) for fill in sorted(fills, key=lambda fill: fill.source_ref or "")]
    snapshot_sha256 = (
        sha256(
            json.dumps(
                canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if canonical
        else None
    )
    preview_rows = [
        {
            "account_id": fill.account_id,
            "date": fill.date,
            "time": fill.time,
            "symbol": fill.symbol,
            "name": fill.name,
            "side": fill.side,
            "qty": fill.qty,
            "price": fill.price,
            "amount": fill.amount,
            "fee": fill.fee,
        }
        for fill in fills[:20]
    ]
    return {
        "available": True,
        "snapshot_sha256": snapshot_sha256,
        "row_count": len(transactions),
        "importable_count": len(fills),
        "skipped_count": sum(skipped.values()),
        "accounts": accounts,
        "preview_rows": preview_rows,
        "warnings": warnings,
    }, fills


def _fhold_transaction_to_fill(transaction: object) -> tuple[Fill | None, str | None]:
    if not isinstance(transaction, dict):
        return None, "交易记录格式无效"
    transaction_id = str(transaction.get("id") or "").strip()
    account_id = str(transaction.get("account_id") or "").strip()
    symbol = _fhold_symbol(transaction.get("code"))
    side = _fhold_side(transaction.get("trade_type"))
    trade_date = _fhold_date(transaction.get("trade_date"))
    trade_time = _fhold_time(transaction.get("trade_time"))
    qty = _finite_float(transaction.get("quantity"))
    price = _finite_float(transaction.get("price"))
    raw_fee, fee_valid = _optional_finite_float(transaction.get("fee"))
    raw_amount, amount_valid = _optional_finite_float(transaction.get("amount"))
    raw_trade_amount, trade_amount_valid = _optional_finite_float(transaction.get("trade_amount"))
    if not transaction_id:
        return None, "缺少稳定交易标识"
    if not account_id:
        return None, "缺少账户标识"
    if side is None:
        # 非普通买卖(融券/银证转账/除权除息等)是语义排除, 先于代码映射报告,
        # 避免把公司行为流水误标成 "证券代码无法映射"。
        return None, "买卖方向不支持"
    if symbol is None:
        return None, "证券代码无法映射"
    if trade_date is None:
        return None, "成交日期无效"
    if trade_time is None:
        return None, "成交时间无效"
    if qty is None or qty <= 0 or price is None or price <= 0:
        return None, "成交数量或价格无效"
    if not fee_valid:
        return None, "手续费无效"
    if not amount_valid or not trade_amount_valid:
        return None, "发生金额无效"

    fee = abs(raw_fee or 0.0)
    if raw_amount is not None and abs(raw_amount) > 0:
        cash_amount = abs(raw_amount)
    elif raw_trade_amount is not None and abs(raw_trade_amount) > 0:
        gross = abs(raw_trade_amount)
        cash_amount = gross + fee if side == "buy" else max(gross - fee, 0.0)
    else:
        gross = qty * price
        cash_amount = gross + fee if side == "buy" else max(gross - fee, 0.0)
    if not isfinite(cash_amount) or cash_amount <= 0:
        return None, "发生金额无效"

    return Fill(
        date=trade_date,
        time=trade_time,
        symbol=symbol,
        name=str(transaction.get("name") or symbol).strip() or symbol,
        side=side,
        qty=qty,
        price=price,
        amount=-cash_amount if side == "buy" else cash_amount,
        fee=fee,
        account_id=f"fhold:{account_id}",
        source_ref=f"fhold:transaction:{transaction_id}",
    ), None


def _fhold_symbol(raw: object) -> str | None:
    from app.services.trading.fhold_client import to_symbol

    return to_symbol(str(raw or ""))


def _fhold_side(raw: object) -> str | None:
    value = str(raw or "").strip().lower()
    if value in {"buy", "买入"}:
        return "buy"
    if value in {"sell", "卖出"}:
        return "sell"
    return None


def _fhold_date(raw: object) -> str | None:
    # fhold 源自券商 xlsx, trade_date 常带时间部分 ("2026-08-14 15:32:05"),
    # 取首个日期 token 再规范化; 时间由 _fhold_time 单独校验。
    value = str(raw or "").strip().replace("/", "-")
    token = value.split(" ")[0].split("T")[0]
    try:
        return date.fromisoformat(token).isoformat()
    except ValueError:
        return None


def _fhold_time(raw: object) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return "00:00:00"
    parts = value.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        hour, minute = (int(parts[0]), int(parts[1]))
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _finite_float(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) and abs(value) <= _MAX_FHOLD_NUMERIC_ABS else None


def _optional_finite_float(raw: object) -> tuple[float | None, bool]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, True
    value = _finite_float(raw)
    return value, value is not None


def _merge_source(
    new_fills: list[Fill],
    new_events: list[CashEvent],
    append: bool,
    import_meta: dict,
) -> tuple[dict, list[Fill], list[CashEvent], int, int, int]:
    old = store.read_source(settings.data_dir) if append else None
    old_fills = [Fill(**row) for row in (old or {}).get("fills", [])]
    old_events = [CashEvent(**row) for row in (old or {}).get("events", [])]
    old_by_source_ref = {fill.source_ref: fill for fill in old_fills if fill.source_ref}
    conflicting_fills = sum(
        1
        for fill in new_fills
        if fill.source_ref in old_by_source_ref
        and _fill_payload_key(old_by_source_ref[fill.source_ref]) != _fill_payload_key(fill)
    )
    fills, deduped_fills = _dedupe(old_fills + new_fills, _fill_key)
    events, deduped_events = _dedupe(old_events + new_events, _event_key)
    source = {
        "imports": [*((old or {}).get("imports", [])), import_meta],
        "fills": [asdict(fill) for fill in fills],
        "events": [asdict(event) for event in events],
    }
    return source, fills, events, deduped_fills, deduped_events, conflicting_fills


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
    if fill.source_ref:
        return ("source_ref", fill.source_ref)
    return _fill_payload_key(fill)


def _fill_payload_key(fill: Fill) -> tuple:
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
    flags = [
        name for name, item in diagnosis.items() if isinstance(item, dict) and item.get("flag")
    ]
    excess = benchmark.get("excess")
    excess_text = "暂无基准超额" if excess is None else f"基准超额 {excess:.1%}"
    flag_text = ", 需重点复盘: " + "、".join(flags) if flags else ", 未触发主要行为风险标签"
    return (
        f"本次合并台账共 {summary.total_trips} 个完成回合, "
        f"胜率 {summary.win_rate:.1%}, 总盈亏 {summary.total_pnl:.2f}, {excess_text}{flag_text}。"
    )


def _market_context(
    repo, benchmark: str, start: str, end: str
) -> tuple[list[str] | None, dict[str, float]]:
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
    return {
        "pnl": t.pnl,
        "total_pnl": t.total_pnl,
        "pnl_pct": t.pnl_pct,
        "buy_avg": t.buy_avg,
        "sell_avg": t.sell_avg,
    }


def _benchmark_name(symbol: str) -> str:
    for item in BENCHMARKS:
        if item["symbol"] == symbol:
            return item["name"]
    return symbol


def _normalize_benchmark(symbol: str) -> str:
    from app.data_providers.fquant.symbols import canonical_index_symbol

    canonical = canonical_index_symbol(symbol)
    allowed = {item["symbol"] for item in BENCHMARKS}
    return canonical if canonical in allowed else "000300.INDEX"
