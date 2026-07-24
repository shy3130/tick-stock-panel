"""Dow monitor configuration and read-only monitor state APIs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, StrictBool

from app.market_rules import market_for_symbol
from app.services.dow_monitor_service import DowMonitorService

router = APIRouter(prefix="/api/dow-monitor", tags=["dow-monitor"])

Market = Literal["all", "cn", "hk", "us"]
Timeframe = Literal["5m", "15m", "30m", "60m", "day"]


class AddSymbolRequest(BaseModel):
    symbol: str
    enabled: StrictBool = True


class PatchSymbolRequest(BaseModel):
    enabled: StrictBool


def _service(request: Request) -> DowMonitorService:
    service = getattr(request.app.state, "dow_monitor_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dow monitor is not initialized")
    return service


def _symbol_and_market(raw_symbol: str) -> tuple[str, Literal["cn", "hk", "us"]]:
    symbol = str(raw_symbol).strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    try:
        return symbol, market_for_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/symbols")
def list_symbols(request: Request) -> dict:
    return {
        "symbols": [item.model_dump(mode="json") for item in _service(request).store.list_symbols()]
    }


@router.post("/symbols")
def add_symbol(payload: AddSymbolRequest, request: Request) -> dict:
    service = _service(request)
    symbol, market = _symbol_and_market(payload.symbol)
    for existing in service.store.list_symbols():
        if existing.symbol == symbol:
            return existing.model_dump(mode="json")
    return service.store.upsert_symbol(symbol, market, payload.enabled).model_dump(mode="json")


@router.delete("/symbols/{symbol}")
def delete_symbol(symbol: str, request: Request) -> dict:
    normalized, _ = _symbol_and_market(symbol)
    if not _service(request).store.remove_symbol(normalized):
        raise HTTPException(status_code=404, detail="Monitored symbol was not found")
    return {"symbol": normalized, "removed": True}


@router.patch("/symbols/{symbol}")
def patch_symbol(symbol: str, payload: PatchSymbolRequest, request: Request) -> dict:
    service = _service(request)
    normalized, market = _symbol_and_market(symbol)
    if not any(item.symbol == normalized for item in service.store.list_symbols()):
        raise HTTPException(status_code=404, detail="Monitored symbol was not found")
    return service.store.upsert_symbol(normalized, market, payload.enabled).model_dump(mode="json")


@router.get("/overview")
def overview(request: Request, market: Market = "all") -> dict:
    return _service(request).overview(market)


@router.get("/notifications")
def notifications(
    request: Request,
    market: Market = "all",
    unread_only: bool = Query(False, alias="unreadOnly"),
) -> dict:
    selected_market = None if market == "all" else market
    rows = _service(request).store.list_notifications(
        market=selected_market,
        unread_only=unread_only,
    )
    return {"notifications": [item.model_dump(mode="json") for item in rows]}


@router.patch("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, request: Request) -> dict:
    service = _service(request)
    if service.store.get_notification(notification_id) is None:
        raise HTTPException(status_code=404, detail="Notification was not found")
    if not service.store.mark_read(notification_id):
        raise HTTPException(status_code=404, detail="Notification was not found")
    notification = service.store.get_notification(notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification was not found")
    return notification.model_dump(mode="json")


@router.get("/status")
def status(request: Request) -> dict:
    return _service(request).status()


@router.get("/{symbol}")
def detail(symbol: str, timeframe: Timeframe, request: Request) -> dict:
    normalized, _ = _symbol_and_market(symbol)
    result = _service(request).detail(normalized, timeframe)
    if result is None:
        raise HTTPException(status_code=404, detail="Monitor state was not found")
    return result
