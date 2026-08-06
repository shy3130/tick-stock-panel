"""单笔交易生命周期状态机 — 纯函数,非法迁移抛 LifecycleError。

状态机:
    open(建档) → 计划中
      → prepare/revise(建仓准备/修订,可重复)
      → fill(确认成交,只能一次) → 持仓中
      → add/tp/sl/adjust(可重复)
      → close(全部平仓) → 已平仓(终态,拒绝一切后续写入)

仓位口径:
- invested = Σ 实际买入金额 (服务端按 qty×price 重算,不信任客户端金额)
- 部分卖出后成本价不变,invested 按剩余股数等比例缩减
- realizedPnl 在 tp/sl/close 时按 (卖出价-成本价)×股数 累加
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.trading.models import (
    KIND_ADD,
    KIND_ADJUST,
    KIND_CLOSE,
    KIND_FILL,
    KIND_OPEN,
    KIND_PREPARE,
    KIND_REVISE,
    KIND_SL,
    KIND_TP,
    LifecycleError,
    STATUS_CLOSED,
    STATUS_HOLDING,
    STATUS_PLANNED,
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
        "status": STATUS_PLANNED,
        "strategy": str(payload.get("strategy") or "").strip() or None,
        "thesis": {"text": text, "invalidation": invalidation, "createdAt": ts},
        "stopLoss": stop_loss,
        "position": {"qty": 0.0, "costPrice": 0.0, "invested": 0.0},
        "realizedPnl": 0.0,
        "createdAt": ts,
        "closedAt": None,
    }


def apply_event(trade: dict[str, Any], kind: str, payload: dict[str, Any], ts: str) -> dict[str, Any]:
    """把一个事件应用到单笔事实上,返回更新后的 trade(不持久化)。

    所有非法迁移在此拒绝 —— 这是结构红线,不允许只在前端校验。
    """
    if trade.get("status") == STATUS_CLOSED:
        raise LifecycleError("该笔交易已平仓归档,拒绝任何后续写入")

    handler = {
        KIND_PREPARE: _apply_prepare,
        KIND_REVISE: _apply_prepare,
        KIND_FILL: _apply_fill,
        KIND_ADD: _apply_add,
        KIND_TP: _apply_sell,
        KIND_SL: _apply_sell,
        KIND_ADJUST: _apply_adjust,
        KIND_CLOSE: _apply_close,
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
    planned_qty = _opt_positive(payload.get("plannedQty"), "plannedQty")
    planned_price = _opt_positive(payload.get("plannedPrice"), "plannedPrice")
    if kind == KIND_PREPARE:
        trade["plan"] = {"qty": planned_qty, "price": planned_price, "ts": ts}
    else:
        trade.setdefault("planRevisions", []).append(
            {"qty": planned_qty, "price": planned_price, "ts": ts}
        )
    stop_loss = _opt_positive(payload.get("stopLoss"), "stopLoss")
    if stop_loss is not None:
        trade["stopLoss"] = stop_loss
    return trade


def _apply_fill(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    if trade["status"] != STATUS_PLANNED:
        raise LifecycleError("fill 只能发生一次,且必须在 计划中 状态")
    qty = _required_positive(payload.get("qty"), "qty")
    price = _required_positive(payload.get("price"), "price")
    invested = round(qty * price, 2)  # 服务端重算,不信任客户端金额
    trade["position"] = {"qty": qty, "costPrice": price, "invested": invested}
    trade["status"] = STATUS_HOLDING
    payload["invested"] = invested  # 回写事件载荷,保证事件流自洽
    return trade


def _apply_add(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    _require_holding(trade, kind)
    if payload.get("planOnly"):
        return trade  # 加仓计划只追加事件,不改变事实
    qty = _required_positive(payload.get("qty"), "qty")
    price = _required_positive(payload.get("price"), "price")
    pos = trade["position"]
    new_qty = pos["qty"] + qty
    new_invested = round(pos["invested"] + qty * price, 2)
    trade["position"] = {
        "qty": new_qty,
        "costPrice": round(new_invested / new_qty, 4),
        "invested": new_invested,
    }
    return trade


def _apply_sell(trade: dict, kind: str, payload: dict, ts: str) -> dict:
    """tp/sl 部分卖出。全部退出必须走 close(终态),保证红旗与复盘口径一致。"""
    _require_holding(trade, kind)
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
    _require_holding(trade, kind)
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
    _require_holding(trade, kind)
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


# ── 工具 ─────────────────────────────────────────────────
def _require_holding(trade: dict, kind: str) -> None:
    if trade["status"] != STATUS_HOLDING:
        raise LifecycleError(f"{kind} 只允许作用于 持仓中 且已有成交的单笔")


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
