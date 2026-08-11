"""单笔交易生命周期状态机 — 纯函数，非法迁移抛 LifecycleError。

状态机:
    open → 计划中
      → prepare/revise
      → fill(可重复，complete=false) → 建仓中
      → fill(complete=true/finalizeOnly) → 持仓中
      → add/trim 调整建仓计划；tp/sl/adjust/close 管理真实仓位
    零成交的计划中 → void → 已作废

仓位与计划是两套事实:
- position.invested 是当前剩余仓位成本，会随减仓下降；
- build.filledAmount 是累计建仓成交金额，不因减仓回退；
- plan.total 是当前建仓计划总额，不得低于 build.filledAmount。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.trading.models import (
    KIND_ADD,
    KIND_ADJUST,
    KIND_CLOSE,
    KIND_FILL,
    KIND_PREPARE,
    KIND_REVISE,
    KIND_SL,
    KIND_TP,
    KIND_TRIM,
    KIND_VOID,
    STATUS_BUILDING,
    STATUS_CLOSED,
    STATUS_HOLDING,
    STATUS_PLANNED,
    STATUS_VOIDED,
    LifecycleError,
)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def new_trade(trade_id: str, symbol: str, payload: dict[str, Any], ts: str) -> dict[str, Any]:
    """open 建档:返回单笔初始事实。thesis.invalidation 必填(可观察反证)。"""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise LifecycleError("建档必须提供标的名称 name")
    thesis = payload.get("thesis") or {}
    text = str(thesis.get("text") or "").strip()
    invalidation = str(thesis.get("invalidation") or "").strip()
    if not text:
        raise LifecycleError("建档必须提供买入论点 thesis.text")
    if not invalidation:
        raise LifecycleError("建档必须提供可观察的失效信号 thesis.invalidation")
    stop_loss = _opt_positive(payload.get("stopLoss"), "stopLoss")
    return {
        "schemaVersion": 1,
        "tradeId": trade_id,
        "symbol": symbol,
        "name": name,
        "accountId": str(payload.get("accountId") or "default").strip() or "default",
        "status": STATUS_PLANNED,
        "strategy": str(payload.get("strategy") or "").strip() or None,
        "thesis": {"text": text, "invalidation": invalidation, "createdAt": ts},
        "stopLoss": stop_loss,
        "position": {"qty": 0.0, "costPrice": 0.0, "invested": 0.0},
        "build": {"filledQty": 0.0, "filledAmount": 0.0, "fillCount": 0, "completedAt": None},
        "realizedPnl": 0.0,
        "createdAt": ts,
        "closedAt": None,
        "voidedAt": None,
    }


def apply_event(trade: dict[str, Any], kind: str, payload: dict[str, Any], ts: str) -> dict[str, Any]:
    """把一个事件应用到单笔事实上,返回更新后的 trade(不持久化)。

    所有非法迁移在此拒绝 —— 这是结构红线,不允许只在前端校验。
    """
    if trade.get("status") in (STATUS_CLOSED, STATUS_VOIDED):
        raise LifecycleError(f"该笔交易{trade.get('status')}，拒绝任何后续写入")

    handler = {
        KIND_PREPARE: _apply_prepare,
        KIND_REVISE: _apply_prepare,
        KIND_FILL: _apply_fill,
        KIND_ADD: _apply_plan_change,
        KIND_TRIM: _apply_plan_change,
        KIND_TP: _apply_sell,
        KIND_SL: _apply_sell,
        KIND_ADJUST: _apply_adjust,
        KIND_CLOSE: _apply_close,
        KIND_VOID: _apply_void,
    }.get(kind)
    if handler is None:
        raise LifecycleError(f"未知事件类型: {kind}")
    return handler(trade, kind, payload, ts)


def has_prepare_or_revise(events: list[dict[str, Any]]) -> bool:
    return any(e.get("kind") in (KIND_PREPARE, KIND_REVISE) for e in events)


# ── 各事件处理 ───────────────────────────────────────────
def _apply_prepare(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    if trade["status"] != STATUS_PLANNED:
        raise LifecycleError(f"{kind} 只允许作用于 计划中 的单笔")
    current = dict(trade.get("plan") or {})
    planned_qty = _opt_positive(payload.get("plannedQty"), "plannedQty")
    planned_price = _opt_positive(payload.get("plannedPrice"), "plannedPrice")
    planned_total = _opt_positive(payload.get("plannedAmount"), "plannedAmount")
    if planned_qty is not None:
        current["qty"] = planned_qty
    if planned_price is not None:
        current["price"] = planned_price
    if planned_total is None and current.get("qty") and current.get("price"):
        planned_total = round(float(current["qty"]) * float(current["price"]), 2)
    if planned_total is not None:
        current["total"] = planned_total
    current["ts"] = ts
    if kind == KIND_PREPARE:
        trade["plan"] = current
    else:
        trade.setdefault("planRevisions", []).append(dict(current))
        trade["plan"] = current
    stop_loss = _opt_positive(payload.get("stopLoss"), "stopLoss")
    if stop_loss is not None:
        trade["stopLoss"] = stop_loss
    return trade


def _apply_fill(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    if trade["status"] not in (STATUS_PLANNED, STATUS_BUILDING, STATUS_HOLDING):
        raise LifecycleError("fill 只允许发生在 计划中/建仓中，或已先 add 调大计划的 持仓中")

    complete = payload.get("complete", False)
    if not isinstance(complete, bool):
        raise LifecycleError("complete 必须是布尔值")
    finalize_only = payload.get("finalizeOnly", False)
    if not isinstance(finalize_only, bool):
        raise LifecycleError("finalizeOnly 必须是布尔值")

    build = _build_facts(trade)
    if trade["status"] == STATUS_HOLDING:
        plan_total = _current_plan_total(trade)
        if plan_total is None or plan_total <= build["filledAmount"]:
            raise LifecycleError("持仓中追加成交前必须先用 add 调大计划总额")
    if finalize_only:
        if trade["status"] != STATUS_BUILDING or build["filledQty"] <= 0:
            raise LifecycleError("finalizeOnly 只允许收口已有成交的 建仓中 单笔")
        if not complete:
            raise LifecycleError("finalizeOnly 必须同时 complete=true")
        payload.update({"complete": True, "finalizeOnly": True, "batchIndex": build["fillCount"]})
        build["completedAt"] = ts
        trade["build"] = build
        trade["status"] = STATUS_HOLDING
        return trade

    qty = _required_positive(payload.get("qty"), "qty")
    price = _required_positive(payload.get("price"), "price")
    invested = round(qty * price, 2)
    pos = trade.get("position") or {"qty": 0.0, "costPrice": 0.0, "invested": 0.0}
    new_qty = float(pos.get("qty") or 0.0) + qty
    new_invested = round(float(pos.get("invested") or 0.0) + invested, 2)
    trade["position"] = {
        "qty": new_qty,
        "costPrice": round(new_invested / new_qty, 4),
        "invested": new_invested,
    }
    before = build["filledAmount"]
    build["filledQty"] = round(build["filledQty"] + qty, 8)
    build["filledAmount"] = round(before + invested, 2)
    build["fillCount"] += 1
    build["completedAt"] = ts if complete else None
    trade["build"] = build
    trade["status"] = STATUS_HOLDING if complete else STATUS_BUILDING

    plan_total = _current_plan_total(trade)
    remaining = max(0.0, plan_total - build["filledAmount"]) if plan_total is not None else None
    progress = build["filledAmount"] / plan_total if plan_total and plan_total > 0 else None
    payload.update({
        "invested": invested,
        "batchIndex": build["fillCount"],
        "complete": complete,
        "finalizeOnly": False,
        "filledBefore": before,
        "filledAfter": build["filledAmount"],
        "remaining": round(remaining, 2) if remaining is not None else None,
        "progressPct": round(progress * 100, 2) if progress is not None else None,
    })
    return trade


def _apply_plan_change(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    _require_position(trade, kind)
    if kind == KIND_TRIM and trade["status"] != STATUS_BUILDING:
        raise LifecycleError("trim 只允许作用于 建仓中 的单笔")
    new_total = _required_positive(payload.get("newTotal"), "newTotal")
    old_total = _current_plan_total(trade)
    build = _build_facts(trade)
    filled = build["filledAmount"]
    if old_total is None:
        old_total = filled
    if kind == KIND_ADD and new_total <= old_total:
        raise LifecycleError("add 的 newTotal 必须大于当前建仓计划")
    if kind == KIND_TRIM:
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise LifecycleError("trim 必须提供非空 reason")
        if new_total >= old_total:
            raise LifecycleError("trim 的 newTotal 必须小于当前建仓计划")
    if new_total < filled:
        raise LifecycleError(f"建仓计划 {new_total:g} 不得低于累计已成交金额 {filled:g}")

    trade["plan"] = {**(trade.get("plan") or {}), "total": new_total, "ts": ts}
    settles = abs(new_total - filled) < 0.005
    payload.update({
        "planOnly": True,
        "changesPositionFacts": False,
        "previousTotal": old_total,
        "newTotal": new_total,
        "delta": round(new_total - old_total, 2),
        "direction": "increase" if kind == KIND_ADD else "decrease",
        "filledAmount": filled,
        "filledShares": build["filledQty"],
        "settlesBuild": settles,
    })
    if settles:
        trade["status"] = STATUS_HOLDING
        build["completedAt"] = ts
    else:
        trade["status"] = STATUS_BUILDING
        build["completedAt"] = None
    trade["build"] = build
    return trade


def _apply_sell(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    """tp/sl 部分卖出。全部退出必须走 close(终态),保证红旗与复盘口径一致。"""
    _require_position(trade, kind)
    qty = _required_positive(payload.get("qty"), "qty")
    price = _required_positive(payload.get("price"), "price")
    pos = trade["position"]
    if qty > pos["qty"]:
        raise LifecycleError(f"卖出股数 {qty} 超过当前持仓 {pos['qty']}")
    if qty == pos["qty"]:
        raise LifecycleError("全部退出必须使用 close 事件,保证平仓归档口径一致")
    remaining = pos["qty"] - qty
    trade["realizedPnl"] = round(trade["realizedPnl"] + (price - pos["costPrice"]) * qty, 2)
    trade["position"] = {
        "qty": remaining,
        "costPrice": pos["costPrice"],
        "invested": round(remaining * pos["costPrice"], 2),
    }
    return trade


def _apply_adjust(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    """调整止损/逻辑退出。记录 old→new,是放宽止损红旗的检出输入。"""
    _require_position(trade, kind)
    new_stop = _opt_positive(payload.get("newStopLoss"), "newStopLoss")
    new_rule = str(payload.get("newExitRule") or "").strip()
    if new_stop is None and not new_rule:
        raise LifecycleError("adjust 必须提供 newStopLoss 或 newExitRule")
    payload["oldStopLoss"] = trade.get("stopLoss")  # 服务端补录旧值,防篡改
    if new_stop is not None:
        trade["stopLoss"] = new_stop
    if new_rule:
        trade["exitRule"] = new_rule
    return trade


def _apply_close(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    _require_position(trade, kind)
    price = _required_positive(payload.get("price"), "price")
    pos = trade["position"]
    if pos["qty"] <= 0:
        raise LifecycleError("当前无持仓,无法平仓")
    trade["realizedPnl"] = round(trade["realizedPnl"] + (price - pos["costPrice"]) * pos["qty"], 2)
    payload["qty"] = pos["qty"]  # close 必须卖完全部剩余,股数由服务端补录
    trade["position"] = {"qty": 0.0, "costPrice": 0.0, "invested": 0.0}
    trade["status"] = STATUS_CLOSED
    trade["closedAt"] = ts
    return trade


def _apply_void(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    if trade["status"] != STATUS_PLANNED:
        raise LifecycleError("void 只允许作用于零成交的 计划中 单笔")
    build = _build_facts(trade)
    if build["filledQty"] > 0 or float((trade.get("position") or {}).get("qty") or 0.0) > 0:
        raise LifecycleError("已有真实成交的单笔不得作废，必须走平仓")
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise LifecycleError("void 必须提供非空 reason")
    payload.update({
        "reason": reason,
        "voidDate": ts,
        "plannedAmount": _current_plan_total(trade),
        "accountId": trade.get("accountId") or "default",
        "filledAmount": 0.0,
    })
    trade["status"] = STATUS_VOIDED
    trade["voidedAt"] = ts
    return trade


# ── 工具 ─────────────────────────────────────────────────
def _require_position(trade: dict, kind: str) -> None:
    if trade["status"] not in (STATUS_BUILDING, STATUS_HOLDING):
        raise LifecycleError(f"{kind} 只允许作用于 建仓中/持仓中 且已有成交的单笔")
    if float((trade.get("position") or {}).get("qty") or 0.0) <= 0:
        raise LifecycleError(f"{kind} 要求已有真实持仓")


def _build_facts(trade: dict) -> dict[str, Any]:
    raw = trade.get("build")
    if isinstance(raw, dict):
        return {
            "filledQty": float(raw.get("filledQty") or 0.0),
            "filledAmount": float(raw.get("filledAmount") or 0.0),
            "fillCount": int(raw.get("fillCount") or 0),
            "completedAt": raw.get("completedAt"),
        }
    position = trade.get("position") or {}
    qty = float(position.get("qty") or 0.0)
    return {
        "filledQty": qty,
        "filledAmount": float(position.get("invested") or 0.0),
        "fillCount": 1 if qty > 0 else 0,
        "completedAt": trade.get("createdAt") if trade.get("status") == STATUS_HOLDING else None,
    }


def _current_plan_total(trade: dict) -> float | None:
    plan = trade.get("plan") or {}
    total = plan.get("total")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
        return float(total)
    qty = plan.get("qty")
    price = plan.get("price")
    if (
        isinstance(qty, (int, float))
        and not isinstance(qty, bool)
        and isinstance(price, (int, float))
        and not isinstance(price, bool)
        and qty > 0
        and price > 0
    ):
        return round(float(qty) * float(price), 2)
    return None


def _required_positive(value: Any, field: str) -> float:
    v = _opt_positive(value, field)
    if v is None:
        raise LifecycleError(f"{field} 必须是正数")
    return v


def _opt_positive(value: Any, field: str) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise LifecycleError(f"{field} 必须是数字")
    if v <= 0:
        raise LifecycleError(f"{field} 必须是正数")
    return v
