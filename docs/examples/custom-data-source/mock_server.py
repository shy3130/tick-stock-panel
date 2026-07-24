"""Mock custom market data source for local integration tests.

Run:
    python mock_server.py

Then copy mock_source.yaml to data/data_sources/mock_source.yaml and reload data
sources in the app settings page.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI(title="Mock Custom Market Data Source")

SYMBOLS = {
    "000001.SZ": "平安银行",
    "600000.SH": "浦发银行",
    "300750.SZ": "宁德时代",
}

BASE = {
    "000001.SZ": 10.20,
    "600000.SH": 8.60,
    "300750.SZ": 186.00,
}


def _parse_symbols(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v) in SYMBOLS]
    if isinstance(value, str) and value:
        return [s.strip() for s in value.split(",") if s.strip() in SYMBOLS]
    return list(SYMBOLS)


async def _payload(request: Request) -> dict:
    if request.method == "POST":
        try:
            return await request.json()
        except Exception:
            return {}
    return dict(request.query_params)


def _parse_date(value: Any, fallback: date) -> date:
    if not value:
        return fallback
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return fallback


@app.api_route("/daily", methods=["GET", "POST"])
async def daily(request: Request):
    body = await _payload(request)
    symbols = _parse_symbols(body.get("symbols"))
    end = _parse_date(body.get("end_time"), date.today())
    start = _parse_date(body.get("start_time"), end - timedelta(days=5))
    rows = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            offset = (cur - start).days
            for sym in symbols:
                base = BASE[sym] + offset * 0.03
                rows.append({
                    "ts_code": sym,
                    "trade_date": cur.isoformat(),
                    "open": round(base, 2),
                    "high": round(base * 1.015, 2),
                    "low": round(base * 0.985, 2),
                    "close": round(base * 1.004, 2),
                    "vol": 120000 + offset * 1000,
                    "amt": round((120000 + offset * 1000) * base, 2),
                })
        cur += timedelta(days=1)
    return {"code": 0, "data": rows}


@app.api_route("/adj_factor", methods=["GET", "POST"])
async def adj_factor(request: Request):
    body = await _payload(request)
    symbols = _parse_symbols(body.get("symbols"))
    today = date.today()
    return {
        "code": 0,
        "data": [
            {"ts_code": sym, "trade_date": today.isoformat(), "factor": 1.0}
            for sym in symbols
        ],
    }


@app.api_route("/realtime", methods=["GET", "POST"])
async def realtime():
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for i, (sym, name) in enumerate(SYMBOLS.items()):
        prev = BASE[sym]
        last = round(prev * (1 + (i + 1) * 0.006), 2)
        change = round(last - prev, 2)
        rows.append({
            "ts_code": sym,
            "name": name,
            "last": last,
            "pre_close": prev,
            "open": round(prev * 1.002, 2),
            "high": round(last * 1.01, 2),
            "low": round(prev * 0.99, 2),
            "vol": 150000 + i * 20000,
            "amt": round((150000 + i * 20000) * last, 2),
            "pct": change / prev,
            "amount_change": change,
            "amplitude": 0.025,
            "turnover": 0.012 + i * 0.001,
            "timestamp": now,
            "session": "regular",
        })
    return {"code": 0, "data": rows}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=3021)
