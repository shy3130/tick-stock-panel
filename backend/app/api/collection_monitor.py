"""Read-only same-origin proxy for collection-monitor evidence."""
from __future__ import annotations

import json
import os
from datetime import date as calendar_date

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request

router = APIRouter(prefix="/api/collection-monitor", tags=["collection-monitor"])

_OVERVIEW_PATH = "/api/collection-monitor/overview"
_MARKET_PATH = "/api/collection-monitor/markets/{market}"
_TASKS_PATH = "/api/collection-monitor/tasks"
_GAPS_PATH = "/api/collection-monitor/gaps"
_TIMEOUT_SECONDS = 10.0

_MARKET_PATTERN = "^(cn|hk|us)$"
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_STATUS_PATTERN = "^(green|yellow|red|gray|unavailable)$"
_TECHNOLOGY_PATTERN = "^(rust|websocket|python|batch)$"
_DATASET_PATTERN = "^(capital_distribution|capital_flow|candlestick_1m|depth|trades)$"
_MODE_PATTERN = "^(production|shadow|backfill)$"
_SYMBOL_PATTERN = r"^[A-Z0-9][A-Z0-9._-]{0,31}\.(HK|US|SH|SZ)$"


def _endpoint() -> str:
    return os.getenv("LONGBRIDGE_API_URL", "http://127.0.0.1:19912").rstrip("/")


def _require_only_query_parameters(request: Request, allowed: set[str]) -> None:
    unexpected = set(request.query_params) - allowed
    if unexpected:
        raise HTTPException(status_code=422, detail="unknown collection monitor query parameter")


def _canonical_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        calendar_date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid collection monitor date") from exc
    return value


def _query_parameters(**values: object) -> dict[str, object]:
    return {name: value for name, value in values.items() if value is not None}


def _read(path: str, params: dict[str, object]) -> object:
    try:
        response = httpx.get(f"{_endpoint()}{path}", params=params, timeout=_TIMEOUT_SECONDS)
        if response.status_code == 503:
            raise HTTPException(
                status_code=503,
                detail="collection_monitoring_evidence_unavailable",
            )
        response.raise_for_status()
        payload = response.json()
        json.dumps(payload, allow_nan=False)
        return payload
    except HTTPException:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail="collection_monitoring_proxy_unavailable",
        ) from exc


@router.get("/overview")
def overview(
    request: Request,
    date: str | None = Query(default=None, pattern=_DATE_PATTERN),
) -> object:
    _require_only_query_parameters(request, {"date"})
    return _read(_OVERVIEW_PATH, _query_parameters(date=_canonical_date(date)))


@router.get("/markets/{market}")
def market_overview(
    request: Request,
    market: str = Path(pattern=_MARKET_PATTERN),
    date: str | None = Query(default=None, pattern=_DATE_PATTERN),
) -> object:
    _require_only_query_parameters(request, {"date"})
    return _read(
        _MARKET_PATH.format(market=market),
        _query_parameters(date=_canonical_date(date)),
    )


@router.get("/tasks")
def tasks(
    request: Request,
    date: str | None = Query(default=None, pattern=_DATE_PATTERN),
    status: str | None = Query(default=None, pattern=_STATUS_PATTERN),
    technology: str | None = Query(default=None, pattern=_TECHNOLOGY_PATTERN),
    market: str | None = Query(default=None, pattern=_MARKET_PATTERN),
    dataset: str | None = Query(default=None, pattern=_DATASET_PATTERN),
    mode: str | None = Query(default=None, pattern=_MODE_PATTERN),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100000),
) -> object:
    _require_only_query_parameters(
        request,
        {"date", "status", "technology", "market", "dataset", "mode", "limit", "offset"},
    )
    return _read(
        _TASKS_PATH,
        _query_parameters(
            date=_canonical_date(date),
            status=status,
            technology=technology,
            market=market,
            dataset=dataset,
            mode=mode,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/gaps")
def gaps(
    request: Request,
    market: str = Query(pattern=_MARKET_PATTERN),
    dataset: str = Query(pattern=_DATASET_PATTERN),
    date: str | None = Query(default=None, pattern=_DATE_PATTERN),
    symbol: str | None = Query(default=None, pattern=_SYMBOL_PATTERN),
    recovered: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100000),
) -> object:
    _require_only_query_parameters(
        request,
        {"market", "dataset", "date", "symbol", "recovered", "limit", "offset"},
    )
    return _read(
        _GAPS_PATH,
        _query_parameters(
            market=market,
            dataset=dataset,
            date=_canonical_date(date),
            symbol=symbol,
            recovered=recovered,
            limit=limit,
            offset=offset,
        ),
    )
