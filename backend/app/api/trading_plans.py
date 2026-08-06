"""Trading 计划台 + 门禁 API — gate-rules 读写 / gate 预检 / 计划 CRUD / 偏差。

prefix=/api/trading, tags=["trading-plans"], 与 trading.py 同前缀不冲突 (路径不重叠)。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from app.config import settings
from app.services.trading.gates import evaluate_gates, read_gate_rules, write_gate_rules
from app.services.trading.plans import deviation as plan_deviation
from app.services.trading.plans import read_plan, write_plan
from app.services.trading import store

router = APIRouter(prefix="/api/trading", tags=["trading-plans"])


# ── 用户门禁规则 ─────────────────────────────────────────
@router.get("/gate-rules")
def get_gate_rules():
    return read_gate_rules(settings.data_dir)


@router.put("/gate-rules")
def put_gate_rules(payload: Annotated[dict[str, Any], Body()]):
    try:
        return write_gate_rules(settings.data_dir, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── 门禁预检 (不落盘, 供前端决策台实时预览) ─────────────
@router.post("/gates/evaluate")
def evaluate(payload: Annotated[dict[str, Any], Body()]):
    mode = str(payload.get("mode") or "").strip()
    if not mode:
        raise HTTPException(status_code=400, detail="mode 必填")
    trade_id = str(payload.get("tradeId") or "").strip() or None
    inner = payload.get("payload") or {}
    if not isinstance(inner, dict):
        raise HTTPException(status_code=400, detail="payload 必须是对象")
    trade = None
    if trade_id:
        trade = store.read_trade(settings.data_dir, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="单笔交易不存在")
    return evaluate_gates(settings.data_dir, mode, trade=trade, payload=inner)


# ── 交易计划 CRUD ────────────────────────────────────────
@router.get("/plans/{date}")
def get_plan(date: str):
    plan = read_plan(settings.data_dir, date)
    if plan is None:
        return {"schemaVersion": 1, "date": date, "entries": [], "actualNotes": ""}
    return plan


@router.put("/plans/{date}")
def put_plan(date: str, payload: Annotated[dict[str, Any], Body()]):
    if not (len(date) == 8 and date.isdigit()):
        raise HTTPException(status_code=400, detail="date 必须是 yyyymmdd")
    try:
        return write_plan(settings.data_dir, date, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/plans/{date}/deviation")
def get_deviation(date: str):
    if not (len(date) == 8 and date.isdigit()):
        raise HTTPException(status_code=400, detail="date 必须是 yyyymmdd")
    return plan_deviation(settings.data_dir, date)
