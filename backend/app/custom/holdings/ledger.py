"""Replay actual fills using moving weighted average; transactions are authoritative."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LOCK = threading.RLock()


class Trade(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    traded_at: datetime
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0, strict=True)
    price: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    fee: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=6)
    note: str = Field(default="", max_length=1000)
    order: int = Field(default=0, ge=0)
    voided: bool = False


def replay(trades: list[Trade], today: date) -> dict:
    active: dict[str, dict] = {}
    closed = []
    rows = []
    ids = set()
    if any(t.traded_at.tzinfo is not None for t in trades):
        raise ValueError("成交时间请使用北京时间，不带时区")
    for t in sorted(trades, key=lambda t: (t.traded_at.date(), t.order)):
        if t.id in ids:
            raise ValueError("交易编号重复")
        ids.add(t.id)
        if t.traded_at.tzinfo is not None:
            raise ValueError("成交时间请使用北京时间，不带时区")
        if t.traded_at.date() > today:
            raise ValueError("不能录入未来交易")
        if t.voided:
            continue
        p = active.get(t.symbol)
        if p is None:
            if t.side == "sell":
                raise ValueError(f"{t.symbol} {t.traded_at}: 卖出超过当时持仓")
            p = dict(id=t.id, symbol=t.symbol, qty=0, cost=Decimal(0), realized=Decimal(0),
                     bought=Decimal(0), sold=Decimal(0), opened=t.traded_at.date().isoformat())
            active[t.symbol] = p
        amount = t.price * t.qty
        gain = Decimal(0)
        if t.side == "buy":
            p["cost"] += amount + t.fee
            p["bought"] += amount + t.fee
            p["qty"] += t.qty
        else:
            if t.qty > p["qty"]:
                raise ValueError(f"{t.symbol} {t.traded_at}: 卖出超过当时持仓")
            released = p["cost"] * t.qty / p["qty"]
            gain = amount - t.fee - released
            p["cost"] -= released
            p["qty"] -= t.qty
            p["sold"] += amount - t.fee
            p["realized"] += gain
        rows.append(dict(**t.model_dump(mode="json"), cycle_id=p["id"], balance=p["qty"],
                         avg_cost=float(p["cost"] / p["qty"]) if p["qty"] else None,
                         realized=float(gain)))
        if p["qty"] == 0:
            p["closed"] = t.traded_at.date().isoformat()
            closed.append(p)
            del active[t.symbol]
    positions = []
    for p in [*active.values(), *closed]:
        end = date.fromisoformat(p.get("closed", today.isoformat()))
        positions.append({**{k: float(v) if isinstance(v, Decimal) else v for k, v in p.items()},
                          "avg_cost": float(p["cost"] / p["qty"]) if p["qty"] else None,
                          "holding_days": (end - date.fromisoformat(p["opened"])).days})
    return {"positions": positions, "transactions": rows}


def load(data_dir: Path) -> list[Trade]:
    path = data_dir / "user_data/holdings/ledger.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if data.get("version") != 1:
        raise ValueError("账本版本不支持，拒绝覆盖")
    return [Trade.model_validate(t) for t in data["trades"]]


def save(data_dir: Path, trades: list[Trade], today: date):
    replay(trades, today)
    path = data_dir / "user_data/holdings/ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "trades": [t.model_dump(mode="json") for t in trades]}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
