"""上传文件解析与归一化。"""
from __future__ import annotations

import io

import polars as pl

from app.services.trade_journal.models import CashEvent, Fill

_BUY = {"买入", "证券买入", "担保买入"}
_SELL = {"卖出", "证券卖出", "担保卖出"}
_CASH_KIND = {
    "银行转证券": "transfer_in",
    "证券转银行": "transfer_out",
    "除权除息": "dividend",
    "股息个税征收": "dividend_tax",
    "融券回购": "repo",
    "融券购回": "repo",
    "通用回购逆回购": "repo",
    "通用回购逆回购购回": "repo",
}


def normalize_code(code: str) -> str:
    code = str(code or "").strip()
    if "." in code:
        return code
    if len(code) == 5:
        return f"{code}.HK"
    if code.startswith(("6", "5", "9")):
        return f"{code}.SH"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def read_upload(data: bytes, filename: str, sheet: str | None) -> tuple[list[str], pl.DataFrame]:
    if filename.lower().endswith((".xlsx", ".xls")):
        frames = pl.read_excel(io.BytesIO(data), sheet_id=0, infer_schema_length=0)
        if isinstance(frames, pl.DataFrame):
            return [], frames
        sheets = list(frames.keys())
        pick = sheet if sheet in frames else ("交易记录" if "交易记录" in frames else sheets[0])
        return sheets, frames[pick]
    return [], pl.read_csv(io.BytesIO(data), infer_schema_length=0)


def _f(v: object) -> float:
    try:
        s = str(v).strip().replace(",", "")
        return float(s) if s not in ("", "None", "null", "-") else 0.0
    except (TypeError, ValueError):
        return 0.0


def normalize_rows(
    df: pl.DataFrame,
    mapping: dict[str, str],
) -> tuple[list[Fill], list[CashEvent], list[str]]:
    fills: list[Fill] = []
    events: list[CashEvent] = []
    warnings: list[str] = []
    unknown_cats: set[str] = set()
    inv = {v: k for k, v in mapping.items()}

    def col(row: dict, field: str) -> object:
        src = inv.get(field)
        return row.get(src) if src else None

    for row in df.iter_rows(named=True):
        date = str(col(row, "date") or "").strip()[:10]
        if not date:
            continue
        cat = str(col(row, "category") or "").strip()
        code = str(col(row, "code") or "").strip()
        symbol = normalize_code(code) if code else ""
        amount = _f(col(row, "amount"))

        if cat in _BUY or cat in _SELL:
            fills.append(
                Fill(
                    date=date,
                    time=str(col(row, "time") or "").strip(),
                    symbol=symbol,
                    name=str(col(row, "name") or "").strip(),
                    side="buy" if cat in _BUY else "sell",
                    qty=_f(col(row, "qty")),
                    price=_f(col(row, "price")),
                    amount=amount,
                    fee=_f(col(row, "fee")),
                )
            )
            continue

        kind = _CASH_KIND.get(cat)
        if kind is None:
            unknown_cats.add(cat)
            kind = "other"
        events.append(CashEvent(date=date, symbol=symbol, kind=kind, amount=amount))

    for cat in sorted(unknown_cats):
        warnings.append(f"未识别的交易类别「{cat}」已归为 other 现金事件, 不参与配对")
    return fills, events, warnings
