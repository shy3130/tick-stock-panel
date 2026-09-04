"""Read-only market data capability and query APIs."""
from __future__ import annotations

import re
import threading
import time
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.data_providers.registry import get_active_provider_name, get_provider
from app.json_safe import json_safe

router = APIRouter(prefix="/api/market-data", tags=["market-data"])
_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_CAPABILITY_KEYS = (
    "chip", "moneyflow_daily_stock", "moneyflow_daily_block",
    "moneyflow_minute_stock", "moneyflow_minute_block", "call_auction",
    "transactions", "hk_adjustment", "hk_financial",
)
_provider_instances: dict[str, Any] = {}
_provider_lock = threading.RLock()
_status_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_STATUS_TTL_SECONDS = 60.0


def _symbol(symbol: str) -> str:
    if not _SYMBOL_RE.fullmatch(symbol or ""):
        raise HTTPException(422, "symbol must be a canonical A-share symbol")
    return symbol


def _provider(capability: str):
    """Reuse one provider instance per effective provider selection."""
    try:
        name = get_active_provider_name(capability)
        with _provider_lock:
            provider = _provider_instances.get(name)
            if provider is None:
                provider = get_provider(name)
                _provider_instances[name] = provider
            return provider
    except Exception as exc:
        raise HTTPException(503, f"market data provider unavailable: {exc}") from exc


def _rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dicts"):
        value = value.to_dicts()
    elif isinstance(value, dict):
        value = [value]
    return [json_safe(dict(row)) if isinstance(row, dict) else json_safe(row) for row in value]


def _call(provider: Any, method: str, *args: Any, capability: str) -> list[dict[str, Any]]:
    fn = getattr(provider, method, None)
    if not callable(fn):
        raise HTTPException(503, f"market data capability unavailable: provider lacks {method}")
    try:
        return _rows(fn(*args))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"market data capability unavailable ({capability}): {exc}") from exc


def _status_parts(provider: Any) -> dict[str, dict[str, Any]]:
    """Cache expensive snapshot coverage scans for a short, bounded interval."""
    cache_key = str(getattr(provider, "name", id(provider)))
    now = time.monotonic()
    with _provider_lock:
        cached = _status_cache.get(cache_key)
        if cached is not None and now - cached[0] < _STATUS_TTL_SECONDS:
            return cached[1]

    merged: dict[str, dict[str, Any]] = {}
    for method in (
        "get_chip_status",
        "get_moneyflow_status",
        "get_microstructure_status",
        "get_market_data_status",
    ):
        fn = getattr(provider, method, None)
        if not callable(fn):
            continue
        try:
            part = fn()
            if isinstance(part, dict):
                merged.update(part)
        except Exception:
            continue
    with _provider_lock:
        _status_cache[cache_key] = (time.monotonic(), merged)
    return merged


def _check_status(provider: Any, capability: str) -> None:
    item = _status_parts(provider).get(capability)
    if isinstance(item, dict) and item.get("available") is False:
        raise HTTPException(503, item.get("reason") or f"{capability} unavailable")


def _date_range(start: date, end: date, *, max_days: int = 366 * 5) -> None:
    if start > end:
        raise HTTPException(422, "start must be on or before end")
    if (end - start).days > max_days:
        raise HTTPException(422, "date range exceeds maximum allowed range")


def _as_datetime(value: date):
    from datetime import datetime, time

    return datetime.combine(value, time.min)


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    provider = _provider("daily")
    merged = _status_parts(provider)
    out: dict[str, Any] = {"available": True, "source": getattr(provider, "name", None), "capabilities": {}}
    versions = getattr(provider, "get_dataquery_versions", None)
    if callable(versions):
        out["dataquery_versions"] = json_safe(versions())
    for key in _CAPABILITY_KEYS:
        item = merged.get(key)
        if not isinstance(item, dict):
            item = {"available": False, "source": getattr(provider, "name", None), "reason": "capability status unavailable"}
        else:
            item = dict(item)
            item.setdefault("available", False)
            item.setdefault("source", getattr(provider, "name", None))
            item.setdefault("reason", None if item["available"] else "capability unavailable")
        for field in ("earliest_date", "latest_date", "rows", "symbols"):
            item.setdefault(field, None)
        out["capabilities"][key] = json_safe(item)
    return json_safe(out)


@router.get("/chip/{symbol}")
def chip(
    symbol: str,
    start: date,
    end: date,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> dict[str, Any]:
    symbol = _symbol(symbol)
    _date_range(start, end)
    provider = _provider("daily")
    _check_status(provider, "chip")
    rows = _call(provider, "get_chip_distribution", symbol, start, end, limit, capability="chip")
    return json_safe({"available": True, "source": getattr(provider, "name", None), "symbol": symbol, "start": start, "end": end, "limit": limit, "rows": rows})


@router.get("/moneyflow/stock/{symbol}")
def moneyflow_stock(
    symbol: str,
    start: date,
    end: date,
    freq: Annotated[str, Query()] = "daily",
) -> dict[str, Any]:
    symbol = _symbol(symbol)
    if freq not in {"daily", "minute"}:
        raise HTTPException(422, "freq must be daily or minute")
    _date_range(start, end, max_days=366 * 5 if freq == "daily" else 0)
    if freq == "minute" and start != end:
        raise HTTPException(422, "minute queries must use a single trading date")
    capability = f"moneyflow_{freq}_stock"
    provider = _provider("minute" if freq == "minute" else "daily")
    _check_status(provider, capability)
    rows = _call(provider, "get_moneyflow_stock", symbol, start, end, freq, capability=capability)
    return json_safe({"available": True, "source": getattr(provider, "name", None), "symbol": symbol, "freq": freq, "start": start, "end": end, "rows": rows})

@router.get("/moneyflow/blocks")
def moneyflow_blocks(
    date_: Annotated[date, Query(alias="date")],
    freq: Annotated[str, Query()] = "daily",
    block_type: Annotated[Literal[40, 41, 42] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    if freq not in {"daily", "minute"}:
        raise HTTPException(422, "freq must be daily or minute")
    capability = f"moneyflow_{freq}_block"
    provider = _provider("minute" if freq == "minute" else "daily")
    _check_status(provider, capability)
    rows = _call(provider, "get_moneyflow_blocks", date_, freq, block_type, limit, capability=capability)
    return json_safe({"available": True, "source": getattr(provider, "name", None), "freq": freq, "date": date_, "block_type": block_type, "limit": limit, "rows": rows})


@router.get("/call-auction/{symbol}")
def call_auction(
    symbol: str,
    date_: Annotated[date, Query(alias="date")],
    session: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=20000)] = 5000,
) -> dict[str, Any]:
    symbol = _symbol(symbol)
    if session is not None and session not in {"open", "close"}:
        raise HTTPException(422, "session must be open or close")
    provider = _provider("realtime")
    _check_status(provider, "call_auction")
    rows = _call(
        provider,
        "get_call_auction",
        symbol,
        _as_datetime(date_),
        session,
        limit,
        capability="call_auction",
    )
    return json_safe({"available": True, "source": getattr(provider, "name", None), "symbol": symbol, "date": date_, "session": session, "limit": limit, "rows": rows})


@router.get("/transactions/{symbol}")
def transactions(
    symbol: str,
    date_: Annotated[date, Query(alias="date")],
    limit: Annotated[int, Query(ge=1, le=20000)] = 5000,
) -> dict[str, Any]:
    symbol = _symbol(symbol)
    provider = _provider("realtime")
    _check_status(provider, "transactions")
    rows = _call(
        provider,
        "get_transactions",
        symbol,
        _as_datetime(date_),
        limit,
        capability="transactions",
    )
    return json_safe({"available": True, "source": getattr(provider, "name", None), "symbol": symbol, "date": date_, "limit": limit, "rows": rows})
