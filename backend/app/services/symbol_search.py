from __future__ import annotations

from typing import Any

import polars as pl

from app.services import eastmoney_client

_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"


def search_symbols(repo, query: str, limit: int = 20) -> list[dict]:
    q = query.strip()
    if not q:
        return []
    limit = max(1, min(50, int(limit)))
    local = _search_local(repo, q, limit)
    if len(local) >= limit:
        return local
    seen = {r["symbol"] for r in local}
    out = list(local)
    for row in suggest_symbols(q, limit - len(local)):
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


def _search_local(repo, query: str, limit: int) -> list[dict]:
    frames = []
    for asset_type, getter in (("stock", "get_instruments"), ("index", "get_index_instruments"), ("etf", "get_etf_instruments")):
        df = getattr(repo, getter)()
        if df.is_empty():
            continue
        cols = [c for c in ("symbol", "code", "name") if c in df.columns]
        if not {"symbol", "code"}.issubset(cols):
            continue
        if "name" not in cols:
            df = df.with_columns(pl.lit("").alias("name"))
        frames.append(df.select("symbol", "code", "name").with_columns(pl.lit(asset_type).alias("asset_type")))
    if not frames:
        return []
    df = pl.concat(frames, how="diagonal").unique(subset=["symbol"], keep="first")
    q = query.upper()
    rows = []
    for row in df.to_dicts():
        symbol = str(row["symbol"]).upper()
        code = str(row["code"]).upper()
        name = str(row.get("name") or "")
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
