"""批次登记域 — 薄"批次"页 (持仓提醒): 只生成监控规则, 不做会计。

每行一个买入批次 → 派生两条规则: lot_{id}_p (price 止盈止损) / lot_{id}_d (date 到期提醒)。
记账/加减仓属"交易口径", 不在本模块 (issue #230)。纯函数 + 文件存储 (统一契约 Store,
收口见 app/definitions.py), 不做 API、不做引擎重载。
"""
from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as _date
from pathlib import Path

from app.definitions import JsonDefinitionStore
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine  # 复用条件文本拼装 (静态方法)

# id 需满足规则 id 同款正则, 且为后缀留位: 派生规则 {id}_p/_d 不得超过 40 字符
_ID = monitor_rules.ID_RE
_MAX_ID_LEN = 40 - 2  # 派生规则 id 后缀 "_p" / "_d"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── 校验与归一化 ────────────────────────────────────────
def validate_lot(lot: dict) -> None:
    """校验批次字段, 非法抛 ValueError (中文信息)。"""
    lot_id = lot.get("id")
    if lot_id is not None and (
        not isinstance(lot_id, str) or not _ID.match(lot_id) or len(lot_id) > _MAX_ID_LEN
    ):
        raise ValueError(f"批次 id 非法 (仅小写字母数字下划线, 且需为派生规则 id 留位): {lot_id!r}")
    if not (lot.get("symbol") or "").strip():
        raise ValueError("symbol 不能为空")
    cost = lot.get("cost_price")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
        raise ValueError("cost_price 必须是正数")
    for key, label in (("qty", "数量"), ("target_pct", "止盈%"), ("stop_pct", "止损%")):
        v = lot.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{label} 不能为负数")
    lead = lot.get("lead_days", 0)
    if isinstance(lead, bool) or not isinstance(lead, int) or lead < 0:
        raise ValueError("lead_days 必须是非负整数")
    for key, label in (("buy_date", "买入日期"), ("remind_date", "到期日")):
        raw = lot.get(key)
        if raw not in (None, ""):
            try:
                _date.fromisoformat(raw)
            except ValueError:
                raise ValueError(f"{label} 必须是 YYYY-MM-DD: {raw!r}") from None
    if not (lot.get("target_pct", 0) > 0 or lot.get("stop_pct", 0) > 0 or lot.get("remind_date")):
        raise ValueError("止盈% / 止损% / 到期日 至少设置一项 (否则无监控点)")


def normalize_lot(lot: dict) -> dict:
    """补全默认字段, 返回规范化后的批次 (不校验)。"""
    d = dict(lot)
    d["symbol"] = (d.get("symbol") or "").strip()
    d.setdefault("qty", 0)
    d.setdefault("cost_price", 0)
    d.setdefault("buy_date", None)
    d.setdefault("target_pct", 0)
    d.setdefault("stop_pct", 0)
    d.setdefault("remind_date", None)
    d.setdefault("lead_days", 1)
    d.setdefault("created_at", _now_iso())
    return d


# ── 持久化 (统一契约 Store; normalize_lot 在上方定义) ────
# 只认 lot_*.json — 目录里的其他文件 (误放的导出等) 不进读取面。
_LOT_STORE = JsonDefinitionStore(
    "user_data/lots", glob_pattern="lot_*.json", label="lot", normalize=normalize_lot
)


def load_all(data_dir: Path) -> list[dict]:
    """读取全部批次。损坏的文件被跳过。"""
    return _LOT_STORE.load_all(data_dir)


def save_one(data_dir: Path, lot: dict) -> None:
    _LOT_STORE.save_one(data_dir, lot)


def delete_one(data_dir: Path, lot_id: str) -> bool:
    return _LOT_STORE.delete_one(data_dir, lot_id)


# ── 批次 → 监控规则 (纯映射) ───────────────────────────
def lot_to_rules(lot: dict) -> tuple[dict | None, dict | None]:
    """批次 → (price 止盈止损规则, date 到期规则); 无对应监控点时返回 None。

    纯映射不做 I/O; 规则 id 派生自批次 id ({lot_id}_p/_d), 保证稳定可级联。
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
            # 提前天数由引擎 evaluate_date_rules 统一追加, 这里只放静态部分
            "message": f"批次到期提醒 · {lot['remind_date']}{qty_text}",
            "enabled": True,
            "lot_id": lot_id,
        }
    return price_rule, date_rule
