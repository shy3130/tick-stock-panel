"""集合竞价研究 API。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.backtest.candidates import CandidateStore, CandidateValidationError

router = APIRouter(prefix="/api/auction", tags=["auction"])


def _hub(request: Request):
    hub = getattr(request.app.state, "auction_service", None)
    if hub is None:
        raise HTTPException(503, "竞价服务未启动")
    return hub


def _research(request: Request):
    svc = getattr(request.app.state, "auction_research_service", None)
    if svc is None:
        raise HTTPException(503, "竞价研究服务未启动")
    return svc


@router.get("/status")
def status(request: Request, trade_date: str | None = None):
    return _hub(request).status(trade_date)


@router.get("/rankings")
def rankings(
    request: Request,
    trade_date: str | None = None,
    as_of_ms: int | None = None,
    style: str = Query("momentum"),
    limit: int = Query(50, ge=1, le=200),
):
    return _hub(request).rankings(
        trade_date=trade_date, as_of_ms=as_of_ms, style=style, limit=limit
    )


@router.get("/market")
def market(
    request: Request,
    sort_by: str = Query("涨幅"),
    count: int = Query(200, ge=1, le=500),
    ascending: bool = Query(False),
):
    return _hub(request).market_rank(sort_by=sort_by, count=count, ascending=ascending)


@router.get("/series/{symbol}")
def series(
    symbol: str,
    request: Request,
    trade_date: str | None = None,
    as_of_ms: int | None = None,
):
    return _hub(request).series(symbol, trade_date, as_of_ms)


@router.post("/refresh")
def refresh(request: Request, trade_date: str | None = None):
    return _hub(request).refresh(trade_date)


class SaveCandidateRequest(BaseModel):
    trade_date: str
    as_of_ms: int | None = None
    style: str = "momentum"
    limit: int = Field(20, ge=1, le=100)
    name: str | None = None


@router.post("/candidates")
def save_candidate(body: SaveCandidateRequest, request: Request):
    ranked = _hub(request).rankings(
        trade_date=body.trade_date,
        as_of_ms=body.as_of_ms,
        style=body.style,
        limit=body.limit,
    )
    research = _research(request).run(
        trade_date=body.trade_date,
        as_of_ms=ranked["as_of_ms"],
        style=body.style,
        limit=body.limit,
    )
    store = CandidateStore(request.app.state.datastore.data_dir)
    symbols = [row["symbol"] for row in ranked["rows"]]
    name = body.name or f"竞价 {body.style} {body.trade_date}"
    try:
        item = store.create(
            kind="auction",
            name=name,
            source_id=f"auction-{uuid.uuid4().hex[:12]}",
            config={
                "trade_date": body.trade_date,
                "as_of_ms": ranked["as_of_ms"],
                "style": body.style,
                "symbols": symbols,
                "limit": body.limit,
            },
            metrics={k: v for k, v in research["metrics"].items() if v is not None},
            data_as_of=body.trade_date,
            status="pending",
        )
    except CandidateValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return item
