"""Per-user trade journal with portfolio-derived attribution."""

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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.services.user_storage import path_for
from app.sycee.portfolio import get_portfolio

router = APIRouter(prefix="/api/sycee/trade-reviews", tags=["sycee-trade-reviews"])

MistakeTag = Literal[
    "plan_deviation",
    "late_entry",
    "early_exit",
    "late_exit",
    "oversize",
    "thesis_error",
    "execution",
    "emotional",
]

_TRADE_ID_RE = re.compile(r"^trade_[0-9a-f]{32}$")
_REVIEW_ID_RE = re.compile(r"^trade_review_[0-9a-f]{32}$")
_STRATEGY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{0,120}$")
_ZERO = Decimal("0")
_lock = threading.RLock()


class TradeReviewUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    strategy_id: str = Field(default="", max_length=120)
    entry_reason: str = Field(default="", max_length=3000)
    expectation: str = Field(default="", max_length=3000)
    invalidation: str = Field(default="", max_length=3000)
    exit_reason: str = Field(default="", max_length=3000)
    conclusion: str = Field(default="", max_length=5000)
    mistake_tags: list[MistakeTag] = Field(default_factory=list)

    @field_validator("strategy_id")
    @classmethod
    def validate_strategy_id(cls, value: str) -> str:
        if not _STRATEGY_ID_RE.fullmatch(value):
            raise ValueError("策略 ID 格式无效")
        return value

    @field_validator("mistake_tags")
    @classmethod
    def unique_tags(cls, value: list[MistakeTag]) -> list[MistakeTag]:
        tags = list(dict.fromkeys(value))
        if len(tags) > 8:
            raise ValueError("错误标签最多允许 8 个")
        return tags

    @model_validator(mode="after")
    def require_content(self):
        text_fields = (
            self.strategy_id,
            self.entry_reason,
            self.expectation,
            self.invalidation,
            self.exit_reason,
            self.conclusion,
        )
        if not any(text_fields) and not self.mistake_tags:
            raise ValueError("复盘内容不能为空")
        return self


class TradeReview(TradeReviewUpdate):
    id: str
    trade_id: str
    created_at: str
    updated_at: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _REVIEW_ID_RE.fullmatch(value):
            raise ValueError("复盘记录 ID 无效")
        return value

    @field_validator("trade_id")
    @classmethod
    def validate_trade_id(cls, value: str) -> str:
        if not _TRADE_ID_RE.fullmatch(value):
            raise ValueError("交易记录 ID 无效")
        return value


def _path():
    path = path_for(settings.data_dir, "sycee/trade_reviews.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_unlocked() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("交易复盘文件无法读取,请检查数据文件") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("交易复盘文件版本无效")
    raw_reviews = payload.get("reviews")
    if not isinstance(raw_reviews, list):
        raise RuntimeError("交易复盘文件内容无效")
    try:
        reviews = [TradeReview.model_validate(item).model_dump() for item in raw_reviews]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("交易复盘文件内容无效") from exc
    if len({review["trade_id"] for review in reviews}) != len(reviews):
        raise RuntimeError("交易复盘文件包含重复交易记录")
    return reviews


def _write_unlocked(reviews: list[dict]) -> None:
    path = _path()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps({"version": 1, "reviews": reviews}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _number(value: Decimal) -> float:
    return round(float(value), 8)


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _derive_attributions(trades: list[dict]) -> dict[str, dict]:
    states: dict[str, dict] = {}
    attributions: dict[str, dict] = {}
    # Portfolio exposes newest-first trades; reversing exactly preserves its replay order,
    # including legacy same-second records whose file order is the only stable tiebreaker.
    for trade in reversed(trades):
        symbol = trade["symbol"]
        state = states.setdefault(
            symbol,
            {"quantity": _ZERO, "cost_value": _ZERO, "opened_at": None},
        )
        quantity = _decimal(trade["quantity"])
        price = _decimal(trade["price"])
        fees = _decimal(trade["fees"])
        if trade["side"] == "buy":
            if state["quantity"] == _ZERO:
                state["opened_at"] = trade["trade_date"]
            cost_basis = quantity * price + fees
            state["quantity"] += quantity
            state["cost_value"] += cost_basis
            attributions[trade["id"]] = {
                "cost_basis": _number(cost_basis),
                "realized_pnl": None,
                "return_pct": None,
                "holding_days": None,
                "pnl_result": "planned",
            }
            continue

        average_cost = state["cost_value"] / state["quantity"]
        cost_basis = average_cost * quantity
        realized_pnl = quantity * (price - average_cost) - fees
        opened_at = state["opened_at"] or trade["trade_date"]
        holding_days = max(
            0,
            (date.fromisoformat(trade["trade_date"]) - date.fromisoformat(opened_at)).days,
        )
        state["quantity"] -= quantity
        state["cost_value"] -= cost_basis
        if state["quantity"] == _ZERO:
            state["cost_value"] = _ZERO
            state["opened_at"] = None
        pnl_result = "profit" if realized_pnl > _ZERO else "loss" if realized_pnl < _ZERO else "breakeven"
        attributions[trade["id"]] = {
            "cost_basis": _number(cost_basis),
            "realized_pnl": _number(realized_pnl),
            "return_pct": _number(realized_pnl / cost_basis) if cost_basis > _ZERO else None,
            "holding_days": holding_days,
            "pnl_result": pnl_result,
        }
    return attributions


def list_trade_reviews() -> dict:
    portfolio = get_portfolio()
    trades = portfolio["trades"]
    attributions = _derive_attributions(trades)
    with _lock:
        reviews = _read_unlocked()
    review_by_trade = {review["trade_id"]: review for review in reviews}
    items = [
        {
            "trade": trade,
            "attribution": attributions[trade["id"]],
            "review": review_by_trade.get(trade["id"]),
        }
        for trade in trades
    ]
    trade_ids = {trade["id"] for trade in trades}
    orphaned = sorted(
        (review for review in reviews if review["trade_id"] not in trade_ids),
        key=lambda review: review["updated_at"],
        reverse=True,
    )
    items.extend(
        {"trade": None, "attribution": None, "review": review}
        for review in orphaned
    )
    sell_ids = {trade["id"] for trade in trades if trade["side"] == "sell"}
    return {
        "items": items,
        "summary": {
            "trade_count": len(trades),
            "reviewed_count": len(trade_ids & review_by_trade.keys()),
            "sell_count": len(sell_ids),
            "reviewed_sell_count": len(sell_ids & review_by_trade.keys()),
            "orphaned_count": len(orphaned),
        },
    }


def upsert_trade_review(trade_id: str, update: TradeReviewUpdate) -> dict | None:
    portfolio = get_portfolio()
    if not any(trade["id"] == trade_id for trade in portfolio["trades"]):
        return None
    now = _now()
    with _lock:
        reviews = _read_unlocked()
        for index, review in enumerate(reviews):
            if review["trade_id"] != trade_id:
                continue
            updated = {
                **review,
                **update.model_dump(),
                "updated_at": now,
            }
            reviews[index] = TradeReview.model_validate(updated).model_dump()
            _write_unlocked(reviews)
            return reviews[index]
        review = TradeReview.model_validate(
            {
                "id": f"trade_review_{uuid4().hex}",
                "trade_id": trade_id,
                **update.model_dump(),
                "created_at": now,
                "updated_at": now,
            }
        ).model_dump()
        reviews.append(review)
        _write_unlocked(reviews)
    return review


def delete_trade_review(trade_id: str) -> bool:
    with _lock:
        reviews = _read_unlocked()
        remaining = [review for review in reviews if review["trade_id"] != trade_id]
        if len(remaining) == len(reviews):
            return False
        _write_unlocked(remaining)
    return True


def _valid_trade_id(trade_id: str) -> str:
    if not _TRADE_ID_RE.fullmatch(trade_id):
        raise HTTPException(status_code=400, detail="交易记录 ID 无效")
    return trade_id


@router.get("")
def list_reviews():
    try:
        return list_trade_reviews()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/{trade_id}")
def save_review(trade_id: str, update: TradeReviewUpdate):
    try:
        review = upsert_trade_review(_valid_trade_id(trade_id), update)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    return {"review": review}


@router.delete("/{trade_id}")
def remove_review(trade_id: str):
    try:
        deleted = delete_trade_review(_valid_trade_id(trade_id))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="复盘记录不存在")
    return {"ok": True}
