"""本地模拟账户 API, 不会向任何券商或外部交易系统发送委托。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.api import advisor as advisor_api
from app.services import paper_account


def _is_json_request(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


class BeginnerReadablePaperRoute(APIRoute):
    """Keep paper-route body errors local, documented, and beginner-readable."""

    def get_route_handler(self):
        original_route_handler = super().get_route_handler()

        async def beginner_readable_handler(request: Request):
            if request.method == "POST" and _is_json_request(request):
                try:
                    # Starlette caches this result, so FastAPI reuses it below.
                    # This also catches Python's JSON integer digit-limit ValueError.
                    await request.json()
                except Exception as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "请求内容不是有效 JSON。"
                            "下一步: 请检查格式后重新提交。"
                        ),
                    ) from exc
            try:
                return await original_route_handler(request)
            except RequestValidationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "请求内容必须是包含完整字段的 JSON 对象。"
                        "下一步: 请检查字段格式后重新提交。"
                    ),
                ) from exc

        return beginner_readable_handler


router = APIRouter(
    prefix="/api/paper",
    tags=["paper-account"],
    route_class=BeginnerReadablePaperRoute,
)


class ResetRequest(BaseModel):
    initial_cash: Any = None
    confirmation: Any = None


class TradeRequest(BaseModel):
    symbol: Any = None
    name: Any = None
    side: Any = None
    quantity: Any = None
    price: Any = None
    trade_date: Any = None
    plan_note: Any = None
    invalidation_note: Any = None


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


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
def reset(payload: ResetRequest, request: Request) -> dict:
    try:
        return paper_account.reset_account(
            _data_dir(request),
            initial_cash=payload.initial_cash,
            confirmation=payload.confirmation,
        )
    except paper_account.PaperAccountValidationError as exc:
        _raise_user_error(exc)
    except paper_account.PaperAccountStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/trades")
def trade(payload: TradeRequest, request: Request) -> dict:
    _require_trusted_data_gate(request)
    try:
        return paper_account.record_trade(
            _data_dir(request),
            symbol=payload.symbol,
            name=payload.name,
            side=payload.side,
            quantity=payload.quantity,
            price=payload.price,
            trade_date=payload.trade_date,
            plan_note=payload.plan_note,
            invalidation_note=payload.invalidation_note,
        )
    except paper_account.PaperAccountValidationError as exc:
        _raise_user_error(exc)
    except paper_account.PaperAccountStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
