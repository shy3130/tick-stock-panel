from __future__ import annotations

from collections.abc import Collection
from typing import Any

import polars as pl

from app.services import eastmoney_client
from app.services.pinyin_index import add_pinyin_columns, pinyin_keys

_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"


def search_symbols(
    repo,
    query: str,
    limit: int = 20,
    asset_type: str | None = None,
    use_suggest: bool = True,
    asset_types: Collection[str] | None = None,
    max_limit: int = 50,
) -> list[dict]:
    q = query.strip()
    if not q:
        return []
    limit = max(1, min(int(max_limit), int(limit)))
    requested_types = (
        {asset_types}
        if isinstance(asset_types, str)
        else frozenset(asset_types) if asset_types else ({asset_type} if asset_type else None)
    )
    local = _search_local(repo, q, limit, asset_types=requested_types)
    if len(local) >= limit or not use_suggest:
        return local
    seen = {r["symbol"] for r in local}
    out = list(local)
    for row in suggest_symbols(q, limit - len(local)):
        if requested_types is not None and row.get("asset_type") not in requested_types:
            continue
        if row["symbol"] not in seen:
            out.append(row)
            seen.add(row["symbol"])
        if len(out) >= limit:
            break
    return out


def suggest_symbols(query: str, limit: int = 10) -> list[dict]:
    payload = eastmoney_client.get_json(_SUGGEST_URL, {"input": query, "type": "14", "token": "D43BF722C8FDD23499C706497796EA14", "count": str(limit)})
    items = _extract_items(payload)
    rows = []
    for item in items:
        code = str(item.get("Code") or item.get("code") or item.get("SECURITY_CODE") or "").strip()
        name = str(item.get("Name") or item.get("name") or item.get("SECURITY_NAME") or "").strip()
        symbol, asset_type = _normalize_code(code)
        if not symbol:
            continue
        rows.append({"symbol": symbol, "code": code, "name": name, "asset_type": asset_type, "source": "eastmoney_suggest", "matched_by": "suggest"})
    return rows[:limit]


def _search_local(
    repo,
    query: str,
    limit: int,
    asset_types: Collection[str] | None = None,
) -> list[dict]:
    frames = []
    for current_type, getter in (
        ("stock", "get_instruments"),
        ("index", "get_index_instruments"),
        ("etf", "get_etf_instruments"),
        ("hk", "get_hk_instruments"),
    ):
        if asset_types is not None and current_type not in asset_types:
            continue
        getter_fn = getattr(repo, getter, None)
        if getter_fn is None:
            continue
        df = getter_fn()
        if df.is_empty():
            continue
        cols = [c for c in ("symbol", "code", "name", "name_pinyin", "name_initials") if c in df.columns]
        if not {"symbol", "code"}.issubset(cols):
            continue
        if "name" not in cols:
            df = df.with_columns(pl.lit("").alias("name"))
        df = add_pinyin_columns(df)
        frames.append(
            df.select("symbol", "code", "name", "name_pinyin", "name_initials").with_columns(
                pl.lit(current_type).alias("asset_type"),
            ),
        )
    if not frames:
        return []
    df = pl.concat(frames, how="diagonal").unique(subset=["symbol"], keep="first")
    q = query.upper()
    q_ascii = query.strip().lower()
    q_is_ascii_alpha = q_ascii.isascii() and q_ascii.isalpha()
    rows = []
    for row in df.to_dicts():
        symbol = str(row["symbol"]).upper()
        code = str(row["code"]).upper()
        name = str(row.get("name") or "")
        name_pinyin = str(row.get("name_pinyin") or "")
        name_initials = str(row.get("name_initials") or "")
        if (not name_pinyin or not name_initials) and name:
            name_pinyin, name_initials = pinyin_keys(name)
        matched_by = ""
        score = 99
        if code == q:
            matched_by, score = "code", 0
        elif symbol.startswith(q):
            matched_by, score = "symbol", 1
        elif code.startswith(q):
            matched_by, score = "code", 2
        elif q in symbol or q in code:
            matched_by, score = "code", 3
        elif query in name:
            matched_by, score = "name", 4
        elif q_ascii and name_pinyin.startswith(q_ascii):
            matched_by, score = "pinyin", 5
        elif q_ascii and q_ascii in name_pinyin:
            matched_by, score = "pinyin", 6
        elif q_is_ascii_alpha and name_initials.startswith(q_ascii):
            matched_by, score = "initials", 7
        if matched_by:
            rows.append({**row, "source": "local", "matched_by": matched_by, "_score": score})
    rows.sort(key=lambda r: (r["_score"], str(r["symbol"])))
    return [{k: v for k, v in r.items() if k != "_score"} for r in rows[:limit]]


def _extract_items(payload: dict[str, Any]) -> list[dict]:
    for key in ("QuotationCodeTable", "Data", "data"):
        val = payload.get(key)
        if isinstance(val, dict):
            inner = val.get("Data") or val.get("data")
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    return []


def _normalize_code(code: str) -> tuple[str, str]:
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) == 5:
        return f"{digits}.HK", "hk"
    if len(digits) != 6:
        return "", "unknown"
    if digits.startswith(("6", "5", "9")):
        return f"{digits}.SH", "etf" if digits.startswith("5") else "stock"
    if digits.startswith(("0", "3", "1")):
        return f"{digits}.SZ", "etf" if digits.startswith(("15", "16")) else "stock"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ", "stock"
    return "", "unknown"
