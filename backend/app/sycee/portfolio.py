"""Per-user portfolio trade ledger and derived position API."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.services.user_storage import path_for

router = APIRouter(prefix="/api/sycee/portfolio", tags=["sycee-portfolio"])

TradeSide = Literal["buy", "sell"]

_TRADE_ID_RE = re.compile(r"^trade_[0-9a-f]{32}$")
_SYMBOL_RE = re.compile(r"^[0-9A-Z._-]{2,32}$")
_ZERO = Decimal("0")
_lock = threading.RLock()


def _validate_symbol(value: str) -> str:
    symbol = value.upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("标的代码格式无效")
    return symbol


def _validate_trade_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("交易日期格式无效") from exc
    return parsed.isoformat()


class PortfolioTradeCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, allow_inf_nan=False)

    symbol: str = Field(min_length=2, max_length=32)
    name: str = Field(default="", max_length=80)
    side: TradeSide
    quantity: float = Field(gt=0, le=1_000_000_000)
    price: float = Field(gt=0, le=10_000_000)
    fees: float = Field(default=0, ge=0, le=100_000_000)
    trade_date: str
    note: str = Field(default="", max_length=500)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _validate_symbol(value)

    @field_validator("trade_date")
    @classmethod
    def validate_trade_date(cls, value: str) -> str:
        return _validate_trade_date(value)


class PortfolioTradeUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, allow_inf_nan=False)

    symbol: str | None = Field(default=None, min_length=2, max_length=32)
    name: str | None = Field(default=None, max_length=80)
    side: TradeSide | None = None
    quantity: float | None = Field(default=None, gt=0, le=1_000_000_000)
    price: float | None = Field(default=None, gt=0, le=10_000_000)
    fees: float | None = Field(default=None, ge=0, le=100_000_000)
    trade_date: str | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str | None) -> str | None:
        return _validate_symbol(value) if value is not None else None

    @field_validator("trade_date")
    @classmethod
    def validate_trade_date(cls, value: str | None) -> str | None:
        return _validate_trade_date(value) if value is not None else None


class PortfolioTrade(PortfolioTradeCreate):
    id: str
    created_at: str
    updated_at: str


class PortfolioConflictError(ValueError):
    pass


def _path():
    path = path_for(settings.data_dir, "sycee/portfolio.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_unlocked() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("持仓数据文件无法读取,请检查数据文件") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("持仓数据文件版本无效")
    raw_trades = payload.get("trades")
    if not isinstance(raw_trades, list):
        raise RuntimeError("持仓数据文件内容无效")
    try:
        return [PortfolioTrade.model_validate(item).model_dump() for item in raw_trades]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("持仓数据文件内容无效") from exc


def _write_unlocked(trades: list[dict]) -> None:
    path = _path()
    temp = path.with_suffix(".json.tmp")
    payload = {"version": 1, "trades": trades}
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _number(value: Decimal) -> float:
    return round(float(value), 8)


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _trade_sort_key(trade: dict) -> tuple[str, str]:
    # Python's stable sort preserves file order for legacy records created in the same second.
    return trade["trade_date"], trade["created_at"]


def _build_portfolio(trades: list[dict]) -> dict:
    states: dict[str, dict] = {}
    realized_total = _ZERO

    for trade in sorted(trades, key=_trade_sort_key):
        symbol = trade["symbol"]
        state = states.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": trade["name"],
                "quantity": _ZERO,
                "cost_value": _ZERO,
                "realized_pnl": _ZERO,
                "first_trade_date": trade["trade_date"],
                "last_trade_date": trade["trade_date"],
            },
        )
        if trade["name"]:
            state["name"] = trade["name"]
        state["last_trade_date"] = trade["trade_date"]

        quantity = _decimal(trade["quantity"])
        price = _decimal(trade["price"])
        fees = _decimal(trade["fees"])
        if trade["side"] == "buy":
            state["quantity"] += quantity
            state["cost_value"] += quantity * price + fees
            continue

        available = state["quantity"]
        if quantity > available:
            raise PortfolioConflictError(
                f"{symbol} 在 {trade['trade_date']} 可卖数量为 {_number(available)},"
                f"不能卖出 {_number(quantity)}"
            )
        average_cost = state["cost_value"] / available
        realized = quantity * (price - average_cost) - fees
        state["quantity"] -= quantity
        state["cost_value"] -= average_cost * quantity
        if state["quantity"] == _ZERO:
            state["cost_value"] = _ZERO
        state["realized_pnl"] += realized
        realized_total += realized

    positions = []
    for state in states.values():
        quantity = state["quantity"]
        if quantity <= _ZERO:
            continue
        cost_value = state["cost_value"]
        positions.append(
            {
                "symbol": state["symbol"],
                "name": state["name"],
                "quantity": _number(quantity),
                "average_cost": _number(cost_value / quantity),
                "cost_value": _number(cost_value),
                "realized_pnl": _number(state["realized_pnl"]),
                "first_trade_date": state["first_trade_date"],
                "last_trade_date": state["last_trade_date"],
            }
        )
    positions.sort(key=lambda position: position["symbol"])
    visible_trades = list(reversed(sorted(trades, key=_trade_sort_key)))
    return {
        "trades": visible_trades,
        "positions": positions,
        "summary": {
            "position_count": len(positions),
            "trade_count": len(trades),
            "cost_value": _number(sum((state["cost_value"] for state in states.values()), _ZERO)),
            "realized_pnl": _number(realized_total),
        },
    }


def get_portfolio() -> dict:
    with _lock:
        trades = _read_unlocked()
        return _build_portfolio(trades)


def create_trade(data: PortfolioTradeCreate) -> tuple[dict, dict]:
    now = _now()
    trade = {
        "id": f"trade_{uuid4().hex}",
        **data.model_dump(),
        "created_at": now,
        "updated_at": now,
    }
    with _lock:
        trades = _read_unlocked()
        candidate = [*trades, trade]
        portfolio = _build_portfolio(candidate)
        _write_unlocked(candidate)
    return trade, portfolio


def update_trade(trade_id: str, changes: PortfolioTradeUpdate) -> tuple[dict, dict] | None:
    updates = changes.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise ValueError("没有可更新的内容")
    with _lock:
        trades = _read_unlocked()
        for index, trade in enumerate(trades):
            if trade["id"] != trade_id:
                continue
            updated = PortfolioTrade.model_validate(
                {**trade, **updates, "updated_at": _now()}
            ).model_dump()
            candidate = [*trades]
            candidate[index] = updated
            portfolio = _build_portfolio(candidate)
            _write_unlocked(candidate)
            return updated, portfolio
    return None


def delete_trade(trade_id: str) -> dict | None:
    with _lock:
        trades = _read_unlocked()
        candidate = [trade for trade in trades if trade["id"] != trade_id]
        if len(candidate) == len(trades):
            return None
        portfolio = _build_portfolio(candidate)
        _write_unlocked(candidate)
    return portfolio


def _valid_trade_id(trade_id: str) -> str:
    if not _TRADE_ID_RE.fullmatch(trade_id):
        raise HTTPException(status_code=400, detail="交易记录 ID 无效")
    return trade_id


@router.get("")
def list_portfolio() -> dict:
    return get_portfolio()


@router.post("/trades", status_code=201)
def add_trade(data: PortfolioTradeCreate) -> dict:
    try:
        trade, portfolio = create_trade(data)
    except PortfolioConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"trade": trade, "portfolio": portfolio}


@router.patch("/trades/{trade_id}")
def patch_trade(trade_id: str, changes: PortfolioTradeUpdate) -> dict:
    try:
        result = update_trade(_valid_trade_id(trade_id), changes)
    except PortfolioConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    trade, portfolio = result
    return {"trade": trade, "portfolio": portfolio}


@router.delete("/trades/{trade_id}")
def remove_trade(trade_id: str) -> dict:
    try:
        portfolio = delete_trade(_valid_trade_id(trade_id))
    except PortfolioConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if portfolio is None:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    return {"ok": True, "portfolio": portfolio}
