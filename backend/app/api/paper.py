"""本地模拟账户 API, 不会向任何券商或外部交易系统发送委托。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api import advisor as advisor_api
from app.services import paper_account

router = APIRouter(prefix="/api/paper", tags=["paper-account"])


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        raw = await request.body()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="请求内容无法读取。下一步: 请重新提交 JSON 对象。",
        ) from exc
    if not raw:
        raise HTTPException(
            status_code=400,
            detail="请求内容不能为空。下一步: 请提交完整的 JSON 对象。",
        )
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="请求内容不是有效 JSON。下一步: 请检查格式后重新提交。",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="请求内容必须是 JSON 对象。下一步: 请检查字段格式后重新提交。",
        )
    return payload


def _raise_user_error(exc: Exception) -> None:
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_trusted_data_gate(request: Request) -> None:
    """Re-evaluate persisted receipts/cache for every simulated fill."""
    try:
        recommendations = advisor_api._persisted_recommendations(request, limit=1)
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "当前无法完成数据检查, 已阻止记录模拟成交。"
                "下一步: 请先刷新数据并确认四项可信回执正常。"
            ),
        ) from exc
    gate = (
        recommendations.get("data_gate")
        if isinstance(recommendations, dict)
        and isinstance(recommendations.get("data_gate"), dict)
        else {}
    )
    if gate.get("decision") == "PASS":
        return
    reasons = gate.get("reasons") if isinstance(gate.get("reasons"), list) else []
    actions = (
        gate.get("next_actions")
        if isinstance(gate.get("next_actions"), list)
        else []
    )
    reason = str(reasons[0]) if reasons else "必需数据尚未通过可信度检查"
    next_action = (
        str(actions[0])
        if actions
        else "请先刷新数据并确认四项可信回执正常后再试。"
    )
    raise HTTPException(
        status_code=409,
        detail=f"数据检查未通过, 已阻止记录模拟成交: {reason}。下一步: {next_action}",
    )


@router.get("/account")
def account(request: Request) -> dict:
    try:
        return paper_account.get_account(_data_dir(request))
    except paper_account.PaperAccountStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/reset")
async def reset(request: Request) -> dict:
    payload = await _read_json_object(request)
    try:
        return paper_account.reset_account(
            _data_dir(request),
            initial_cash=payload.get("initial_cash"),
            confirmation=payload.get("confirmation"),
        )
    except paper_account.PaperAccountValidationError as exc:
        _raise_user_error(exc)
    except paper_account.PaperAccountStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/trades")
async def trade(request: Request) -> dict:
    payload = await _read_json_object(request)
    _require_trusted_data_gate(request)
    try:
        return paper_account.record_trade(
            _data_dir(request),
            symbol=payload.get("symbol"),
            name=payload.get("name"),
            side=payload.get("side"),
            quantity=payload.get("quantity"),
            price=payload.get("price"),
            trade_date=payload.get("trade_date"),
            plan_note=payload.get("plan_note"),
            invalidation_note=payload.get("invalidation_note"),
        )
    except paper_account.PaperAccountValidationError as exc:
        _raise_user_error(exc)
    except paper_account.PaperAccountStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
