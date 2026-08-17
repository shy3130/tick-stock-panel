"""批次登记 API — 薄"批次"页, 只为自动生成监控规则, 不做任何会计。

每行一个买入批次 (标的/数量/成本/止盈%/止损%/到期日), 按行自动生成并同步两条
底层监控规则:
  - lot_{id}_p: type=price 止盈止损 (close >= 止盈价 OR close <= 止损价)
  - lot_{id}_d: type=date  到期提醒 (remind_date + lead_days)
不含剩余数量/盈亏/持有天数等账本语义; 批次信息只是监控配置的载体。
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lots", tags=["lots"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "lots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(data_dir: Path, lot_id: str) -> Path:
    return _dir(data_dir) / f"{lot_id}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 校验与归一化 ────────────────────────────────────────
def validate_lot(lot: dict) -> None:
    """校验批次字段, 非法抛 ValueError (中文信息)。"""
    if not (lot.get("symbol") or "").strip():
        raise ValueError("symbol 不能为空")
    cost = lot.get("cost_price")
    if not isinstance(cost, (int, float)) or cost <= 0:
        raise ValueError("cost_price 必须是正数")
    for key, label in (("qty", "数量"), ("target_pct", "止盈%"), ("stop_pct", "止损%")):
        v = lot.get(key, 0)
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{label} 不能为负数")
    lead = lot.get("lead_days", 0)
    if not isinstance(lead, int) or lead < 0:
        raise ValueError("lead_days 必须是非负整数")
    for key, label in (("buy_date", "买入日期"), ("remind_date", "到期日")):
        raw = lot.get(key)
        if raw not in (None, ""):
            try:
                _date.fromisoformat(raw)
            except ValueError:
                raise ValueError(f"{label} 必须是 YYYY-MM-DD: {raw!r}")
    if not (lot.get("target_pct", 0) > 0 or lot.get("stop_pct", 0) > 0 or lot.get("remind_date")):
        raise ValueError("止盈% / 止损% / 到期日 至少设置一项 (否则无监控点)")


def normalize(lot: dict) -> dict:
    """补全默认字段。"""
    d = dict(lot)
    d["symbol"] = (d.get("symbol") or "").strip()
    d.setdefault("qty", 0)
    d.setdefault("buy_date", None)
    d.setdefault("target_pct", 0)
    d.setdefault("stop_pct", 0)
    d.setdefault("remind_date", None)
    d.setdefault("lead_days", 1)
    d.setdefault("created_at", _now_iso())
    return d


# ── 持久化 ─────────────────────────────────────────────
def load_all(data_dir: Path) -> list[dict]:
    """读取全部批次。损坏的文件被跳过。"""
    out: list[dict] = []
    for f in sorted(_dir(data_dir).glob("lot_*.json")):
        try:
            out.append(normalize(json.loads(f.read_text(encoding="utf-8"))))
        except Exception as e:  # noqa: BLE001
            logger.warning("lot load failed %s: %s", f.name, e)
    return out


def save_one(data_dir: Path, lot: dict) -> None:
    p = _path(data_dir, lot["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(lot, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_one(data_dir: Path, lot_id: str) -> bool:
    p = _path(data_dir, lot_id)
    if p.exists():
        p.unlink()
        return True
    return False


# ── 批次 → 监控规则 (纯映射) ───────────────────────────
def lot_to_rules(lot: dict) -> tuple[dict | None, dict | None]:
    """批次 → (止盈止损 price 规则, 到期 date 规则)。无监控点的返回 None。

    纯映射不做 I/O; id 派生自批次 id: {lot_id}_p / {lot_id}_d。
    止盈/止损价由成本价按百分比换算成绝对价, 供 price 规则阈值比较。
    """
    symbol = lot["symbol"]
    lot_id = lot["id"]
    cost = float(lot["cost_price"])
    target = float(lot.get("target_pct", 0))
    stop = float(lot.get("stop_pct", 0))
    qty = float(lot.get("qty", 0) or 0)
    qty_text = f" · {qty:g}股" if qty > 0 else ""

    conds: list[dict] = []
    if target > 0:
        conds.append({"field": "close", "op": ">=", "value": round(cost * (1 + target / 100), 4)})
    if stop > 0:
        conds.append({"field": "close", "op": "<=", "value": round(cost * (1 - stop / 100), 4)})
    price_rule = None
    if conds:
        # 不含标的: rule.name 与推送头部已带
        msg = f"批次止盈止损 · 成本{cost:g}"
        if target > 0:
            msg += f" · 止盈{target:g}%"
        if stop > 0:
            msg += f" · 止损{stop:g}%"
        msg += qty_text
        cond_text = MonitorRuleEngine._format_conditions_text({"logic": "or"}, conds)
        if cond_text:
            msg += f" · {cond_text}"
        price_rule = {
            "id": f"{lot_id}_p",
            "name": f"批次止盈止损 · {symbol}",
            "type": "price",
            "asset_type": "stock",
            "scope": "symbols",
            "symbols": [symbol],
            "conditions": conds,
            "logic": "or",
            "cooldown_seconds": 86400,
            "severity": "warn",
            "message": msg,
            "enabled": True,
            "lot_id": lot_id,
        }

    date_rule = None
    if lot.get("remind_date"):
        lead = int(lot.get("lead_days", 1))
        date_rule = {
            "id": f"{lot_id}_d",
            "name": f"批次到期 · {symbol}",
            "type": "date",
            "asset_type": "stock",
            "scope": "symbols",
            "symbols": [symbol],
            "remind_date": lot["remind_date"],
            "lead_days": lead,
            "cooldown_seconds": 86400,
            "severity": "info",
            # (提前N天) 由引擎 evaluate_date_rules 统一追加, 这里只放静态部分
            "message": f"批次到期提醒 · {lot['remind_date']}{qty_text}",
            "enabled": True,
            "lot_id": lot_id,
        }
    return price_rule, date_rule


def _reload_engine(request: Request) -> None:
    """批次规则保存/删除后重载引擎 — 复用监控规则 API 的共享重载 (含指数纠正)。

    批次只生成 stock 规则, 但重载会遍历全部规则; 若这里另起一套不带指数纠正的
    reload, 内存里被 _sync_engine 纠正为 index 的规则会被打回 stock 而静默失效。
    """
    from app.api.monitor_rules import _sync_engine
    _sync_engine(request)


def sync_lot(request: Request, lot: dict) -> None:
    """写批次文件 + 同步其生成的两条监控规则 + 重载引擎。

    生成规则继承用户默认推送渠道 (webhook_default_channels), 与 RuleEditor 建新规则
    行为一致; 否则批次告警会静默只走应用内, 持仓监控形同虚设。未配置默认渠道则为 []。
    """
    from app.services import preferences

    data_dir = _data_dir(request)
    save_one(data_dir, lot)
    default_channels = preferences.get_webhook_default_channels()
    price_rule, date_rule = lot_to_rules(lot)
    for rid, rule in ((f"{lot['id']}_p", price_rule), (f"{lot['id']}_d", date_rule)):
        if rule is None:
            monitor_rules.delete_one(data_dir, rid)
            continue
        rule.setdefault("webhook_channels", list(default_channels))
        # 保留旧 created_at, 避免每次编辑批次规则在监控中心列表跳位
        existing = monitor_rules.load_one(data_dir, rid)
        if existing and existing.get("created_at"):
            rule["created_at"] = existing["created_at"]
        try:
            monitor_rules.validate(rule)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        monitor_rules.save_one(data_dir, monitor_rules.normalize(rule))
    _reload_engine(request)


# ── API ────────────────────────────────────────────────
class LotModel(BaseModel):
    id: str | None = None
    symbol: str
    qty: float = 0
    cost_price: float
    buy_date: str | None = None
    target_pct: float = 0
    stop_pct: float = 0
    remind_date: str | None = None
    lead_days: int = 1


@router.get("")
def list_lots(request: Request):
    return {"lots": load_all(_data_dir(request))}


@router.post("")
def upsert_lot(lot_in: LotModel, request: Request):
    """新建/更新一个批次。id 缺省时服务端生成 (紧凑, 保证 {id}_p/_d 规则 id ≤ 40 字符)。"""
    lot = lot_in.model_dump()
    if not lot.get("id"):
        lot["id"] = f"lot_{int(time.time() * 1000):x}_{secrets.token_hex(2)}"
    try:
        validate_lot(lot)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    lot = normalize(lot)
    sync_lot(request, lot)
    return {"ok": True, "lot": lot}


@router.delete("/{lot_id}")
def delete_lot(lot_id: str, request: Request):
    data_dir = _data_dir(request)
    deleted = delete_one(data_dir, lot_id)
    monitor_rules.delete_one(data_dir, f"{lot_id}_p")
    monitor_rules.delete_one(data_dir, f"{lot_id}_d")
    if deleted:
        _reload_engine(request)
    return {"ok": True}
