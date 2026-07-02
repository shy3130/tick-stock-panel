"""TDX front-adjusted daily rows -> raw-price daily rows.

The practical rule is mixed:
- use trusted raw oracle rows when available, currently fstore ``t_1_day_klines``;
- fall back to inverse TDX front-adjustment only for dates missing from oracle.
"""
from __future__ import annotations

from typing import Any

PRICE_COLS = ("open", "high", "low", "close", "last_close")
# oracle 额外回填列: 磁盘 CSV 历史行的 volume 被送转比例放大、amount 被复权价重算(可为负),
# fstore cjl/cje 是精确原始值。
MERGE_COLS = PRICE_COLS + ("volume", "amount")


def reconstruct_raw_rows(
    rows: list[dict],
    events: list[dict],
    oracle_rows: list[dict] | None = None,
) -> list[dict]:
    """Return raw-price rows.

    ``rows`` are TDX/engine daily rows. ``events`` are normalized xdxr-like rows
    with ``trade_date`` or ``date`` plus fenhong/fenshu/songzhuangu/peigu.
    ``oracle_rows`` may be fstore raw rows; when a matching date exists its OHLC
    prices win because pure inverse reconstruction is not exact for all fields.
    """
    oracle = {_date(r): r for r in (oracle_rows or []) if _date(r)}
    reconstructed = _inverse_adjust_rows(rows, events)

    out: list[dict] = []
    for row in reconstructed:
        merged = dict(row)
        raw = oracle.get(_date(row))
        if raw:
            for col in MERGE_COLS:
                value = _get_price(raw, col)
                if value is not None:
                    merged[col] = value
        out.append(merged)
    return out


def _inverse_adjust_rows(rows: list[dict], events: list[dict]) -> list[dict]:
    out = [dict(r) for r in rows]
    for event in sorted(events, key=lambda e: str(e.get("trade_date") or e.get("date") or ""), reverse=True):
        if int(event.get("category") or 0) != 1:
            continue
        trade_date = str(event.get("trade_date") or event.get("date") or "")
        if not trade_date:
            continue
        fenhong = _float(event.get("fenhong"))
        fenshu = _float(event.get("fenshu"))
        songzhuangu = _float(event.get("songzhuangu"))
        peigu = _float(event.get("peigu"))
        peigujia = _float(event.get("peigujia"))
        denom = 10.0 + fenshu + songzhuangu + peigu
        if denom == 0:
            continue
        cash = fenhong - peigu * peigujia
        for row in out:
            date = _date(row)
            if not date or date >= trade_date:
                continue
            for col in PRICE_COLS:
                value = row.get(col)
                if value is not None:
                    row[col] = (_float(value) * denom + cash) / 10.0
            # volume 被送转比例放大过(v_adj = v_raw * denom / 10), 精确逆运算;
            # amount 在乘法部分下是不变量(p_adj*v_adj == p_raw*v_raw), 保留原值。
            if row.get("volume") is not None:
                row["volume"] = _float(row["volume"]) * 10.0 / denom
    return out


def _date(row: dict) -> str:
    value = row.get("date", row.get("tdate"))
    return str(value) if value is not None else ""


def _get_price(row: dict, col: str) -> float | None:
    value = row.get(col, row.get(f"oracle_{col}"))
    if value is None:
        return None
    return _float(value)


def _float(value: Any) -> float:
    return float(value or 0)
