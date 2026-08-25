"""账户模型 — 资金基数、append-only 变更与平仓结转台账。

Schema:
{
  "schemaVersion": 1,
  "accounts": [{
    "id": "default", "currency": "CNY",
    "capital": 500000,
    "horizonFundMonths": 12,
    "maxSingleRatio": 0.25,
    "changes": [...],
    "settlements": [{
      "id": "settle:{tradeId}", "tradeId": "...", "realizedPnl": 1200,
      "capitalBefore": 500000, "capitalAfter": 501200
    }]
  }]
}

用户资金变更与服务端平仓结转都只允许追加；settlement id 保证重试幂等。
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_lock = threading.Lock()

_DEFAULT_ACCOUNT: dict[str, Any] = {
    "id": "default",
    "currency": "CNY",
    "capital": 0,
    "horizonFundMonths": 12,
    "maxSingleRatio": 0.25,
    "changes": [],
    "settlements": [],
}


def _path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trading" / "accounts.json"


def _default_payload() -> dict[str, Any]:
    """无文件时的默认结构: 一个 capital=0 的 default 账户。"""
    return {"schemaVersion": SCHEMA_VERSION, "accounts": [json.loads(json.dumps(_DEFAULT_ACCOUNT))]}


def read_accounts(data_dir: Path) -> dict[str, Any]:
    """读取账户。文件不存在 / 损坏 / 为空 → 返回含 default 账户的默认结构。"""
    p = _path(data_dir)
    if not p.exists():
        return _default_payload()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return _default_payload()
    if not text.strip():
        return _default_payload()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _default_payload()
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return _default_payload()
    return data


def _read_persisted_raw(data_dir: Path) -> dict[str, Any] | None:
    """返回真实持久化的 accounts 文档; 文件不存在/空/损坏/结构无效时返回 None。

    这样首次写入不会把 read_accounts 的虚拟 default 当成已有账户基线。
    """
    p = _path(data_dir)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return None
    return data


def _validate_change_amounts(acc: Any) -> None:
    """校验 changes 中的 amount(含首次与新增账户); 拒绝 bool、NaN 和 inf。"""
    if not isinstance(acc, dict):
        return
    aid = str(acc.get("id") or "").strip() or "?"
    for idx, ch in enumerate(acc.get("changes") or []):
        amt = None if not isinstance(ch, dict) else ch.get("amount")
        if isinstance(amt, bool) or not isinstance(amt, (int, float)) or not math.isfinite(amt):
            raise ValueError(f"账户 {aid}: changes[{idx}] amount 必须是数值(拒绝 bool/NaN/inf)")


def write_accounts(data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """校验并原子落盘账户; changes/settlements 历史只允许追加。

    首次(无 accounts.json)写入时不使用虚拟 default 作为基线;
    仅对真实持久化账户执行 id 保留及 capital 增量校验。
    """
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("accounts 必须是非空数组")
    for acc in accounts:
        _validate_account(acc)
    for acc in accounts:
        _validate_change_amounts(acc)

    out: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "accounts": accounts}
    with _lock:
        persisted = _read_persisted_raw(data_dir)
        if persisted is not None:
            _enforce_append_only(persisted, accounts)
        _write_payload(data_dir, out)
    return out


def _validate_account(acc: Any) -> None:
    if not isinstance(acc, dict):
        raise ValueError("account 必须是对象")
    aid = str(acc.get("id") or "").strip()
    if not aid:
        raise ValueError("account.id 必填")

    capital = acc.get("capital")
    if isinstance(capital, bool) or not isinstance(capital, (int, float)):
        raise ValueError(f"账户 {aid}: capital 必须是数值")
    if capital < 0:
        raise ValueError(f"账户 {aid}: capital 不得为负")

    ratio = acc.get("maxSingleRatio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise ValueError(f"账户 {aid}: maxSingleRatio 必须是数值")
    if not (0 < ratio <= 1):
        raise ValueError(f"账户 {aid}: maxSingleRatio 必须在 (0, 1]")

    horizon = acc.get("horizonFundMonths")
    if isinstance(horizon, bool) or not isinstance(horizon, (int, float)):
        raise ValueError(f"账户 {aid}: horizonFundMonths 必须是数值")
    if horizon <= 0:
        raise ValueError(f"账户 {aid}: horizonFundMonths 必须为正")

    changes = acc.get("changes")
    if changes is None:
        acc["changes"] = []
    elif not isinstance(changes, list):
        raise ValueError(f"账户 {aid}: changes 必须是数组")

    settlements = acc.get("settlements")
    if settlements is None:
        acc["settlements"] = []
    elif not isinstance(settlements, list):
        raise ValueError(f"账户 {aid}: settlements 必须是数组")


def _enforce_append_only(existing: dict[str, Any], accounts: list[dict[str, Any]]) -> None:
    """保护现有账户的 append-only 账本。

    - 同一写锁内: 持久化账户的 id 必须全部出现在请求中(不得删除账户)
    - 对每个已有账户: new_capital - old_capital == 本次新增 changes[].amount 总和
    - settlement 已同步写进 changes, 不在这里重复计 settlements
    - 首次写入/新增 account 的 capital 作为基线, 不与自身 changes 合计匹配
    - 使用绝对误差容忍正常浮点噪声, 不放过实际金额差异
    """
    existing_accts = existing.get("accounts") or []
    by_id = {str(a.get("id") or "").strip(): a for a in existing_accts if a.get("id")}

    incoming_ids = {str(a.get("id") or "").strip() for a in accounts if a.get("id")}
    for aid in by_id:
        if aid not in incoming_ids:
            raise ValueError(f"账户 {aid}: 已有账户在请求中缺失, 不允许删除")

    for acc in accounts:
        aid = str(acc.get("id") or "").strip()
        old = by_id.get(aid)
        if not old:
            # 新账户(或首次建档场景), capital 直接作为基线
            continue

        # 延续前缀不可变校验
        for field in ("changes", "settlements"):
            old_rows = old.get(field) or []
            new_rows = acc.get(field) or []
            if len(new_rows) < len(old_rows):
                raise ValueError(f"账户 {aid}: {field} 只允许追加, 传入条数少于现有 {len(old_rows)} 条")
            for index, old_row in enumerate(old_rows):
                if index >= len(new_rows) or new_rows[index] != old_row:
                    raise ValueError(f"账户 {aid}: {field} 历史记录不可改写, 只能在末尾追加")

        # capital 增量必须由本次新增的 changes 解释(不重复计算 settlements)
        old_cap = old.get("capital") or 0
        new_cap = acc.get("capital")
        if isinstance(new_cap, bool) or not isinstance(new_cap, (int, float)) or not math.isfinite(new_cap):
            raise ValueError(f"账户 {aid}: capital 必须是数值")

        old_len = len(old.get("changes") or [])
        new_changes = acc.get("changes") or []
        appended = new_changes[old_len:]

        added = 0.0
        for i, ch in enumerate(appended):
            amt = None if not isinstance(ch, dict) else ch.get("amount")
            if isinstance(amt, bool) or not isinstance(amt, (int, float)) or not math.isfinite(amt):
                raise ValueError(f"账户 {aid}: changes 新增 amount 必须是数值(索引 {old_len + i})")
            added += float(amt)

        delta = float(new_cap) - float(old_cap)
        if not math.isclose(delta, added, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError(
                f"账户 {aid}: capital 增量 {delta} 与本次新增 changes[].amount 总和 {added} 不一致"
            )


def settle_trade(data_dir: Path, trade: dict[str, Any], ts: str) -> dict[str, Any]:
    """把已平仓单笔的累计已实现盈亏结转进资金基数；按 tradeId 幂等。"""
    if trade.get("status") != "已平仓":
        raise ValueError("只有已平仓单笔可以结转")
    trade_id = str(trade.get("tradeId") or "").strip()
    if not trade_id:
        raise ValueError("tradeId 必填")
    account_id = str(trade.get("accountId") or "default").strip() or "default"
    settlement_id = f"settle:{trade_id}"

    with _lock:
        document = read_accounts(data_dir)
        accounts = document.get("accounts") or []
        account = next((item for item in accounts if item.get("id") == account_id), None)
        if account is None:
            raise ValueError(f"账户不存在: {account_id}")
        account.setdefault("changes", [])
        account.setdefault("settlements", [])
        existing = next(
            (row for row in account["settlements"] if row.get("id") == settlement_id),
            None,
        )
        if existing is not None:
            return existing

        realized = round(float(trade.get("realizedPnl") or 0.0), 2)
        before = round(float(account.get("capital") or 0.0), 2)
        after = round(before + realized, 2)
        if after < 0:
            raise ValueError(
                f"账户 {account_id}: 结转后 capital={after:g} 为负，拒绝写入"
            )
        settlement = {
            "id": settlement_id,
            "ts": ts,
            "tradeId": trade_id,
            "symbol": str(trade.get("symbol") or ""),
            "accountId": account_id,
            "realizedPnl": realized,
            "closeDate": trade.get("closedAt") or ts,
            "capitalBefore": before,
            "capitalAfter": after,
        }
        account["capital"] = after
        account["changes"].append({
            "id": settlement_id,
            "ts": ts,
            "amount": realized,
            "reason": f"平仓结转 {trade_id}",
            "kind": "settlement",
            "tradeId": trade_id,
        })
        account["settlements"].append(settlement)
        _write_payload(data_dir, {"schemaVersion": SCHEMA_VERSION, "accounts": accounts})
        return settlement


def settled_trade_ids(accounts_doc: dict[str, Any]) -> set[str]:
    return {
        str(row.get("tradeId"))
        for account in accounts_doc.get("accounts") or []
        for row in account.get("settlements") or []
        if row.get("tradeId")
    }


def _write_payload(data_dir: Path, payload: dict[str, Any]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
