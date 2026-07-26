"""Proxy live multi-timeframe Dow strategy jobs from Longbridge."""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/dow-strategy", tags=["dow-strategy"])


def _endpoint() -> str:
    return os.getenv("LONGBRIDGE_API_URL", "http://127.0.0.1:19912").rstrip("/")


def _payload(response: httpx.Response):
    try:
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Longbridge Dow strategy unavailable: {exc}") from exc


@router.get("/pool")
def pool(
    market: str = Query(pattern="^(cn|hk|us|all)$"),
    limit: int = Query(default=80, ge=1, le=500),
):
    return _payload(httpx.get(
        f"{_endpoint()}/api/workbench",
        params={"market": market, "limit": limit, "strategy": "dow_trend"},
        timeout=30.0,
    ))


@router.post("/runs")
def start_run(payload: dict):
    market = str(payload.get("market") or "").lower()
    if market not in {"cn", "hk", "us"}:
        raise HTTPException(status_code=400, detail="market must be cn, hk or us")
    return _payload(httpx.post(
        f"{_endpoint()}/api/dow-strategy/runs",
        json={"market": market},
        timeout=30.0,
    ))


@router.get("/runs/{run_id}")
def run_status(run_id: str):
    return _payload(httpx.get(
        f"{_endpoint()}/api/dow-strategy/runs/{run_id}",
        timeout=30.0,
    ))


@router.get("/next-day-direction/pool")
def next_day_direction_pool(
    market: str = Query(pattern="^(cn|hk|us|all)$"),
    limit: int = Query(default=80, ge=1, le=500),
):
    return _payload(httpx.get(
        f"{_endpoint()}/api/workbench",
        params={
            "market": market,
            "limit": limit,
            "strategy": "next_day_direction",
            "includeDetail": "false",
        },
        timeout=90.0,
    ))


@router.get("/next-day-direction/{symbol}")
def next_day_direction_detail(symbol: str):
    return _payload(httpx.get(
        f"{_endpoint()}/api/stocks/{symbol.strip().upper()}",
        params={"strategy": "next_day_direction"},
        timeout=90.0,
    ))


@router.get("/{symbol}")
def detail(symbol: str):
    return _payload(httpx.get(
        f"{_endpoint()}/api/dow-strategy/{symbol.strip().upper()}",
        timeout=30.0,
    ))


@router.post("/backtest")
def backtest(payload: dict):
    return _payload(httpx.post(
        f"{_endpoint()}/api/dow-strategy/backtest",
        json=payload,
        timeout=180.0,
    ))
