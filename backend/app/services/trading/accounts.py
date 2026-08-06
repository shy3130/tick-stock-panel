"""账户模型 — data/user_data/trading/accounts.json 单文件读写。

账户是组合快照的结构红线输入 (capital / maxSingleRatio / horizonFundMonths)。
与交易事件流一致,账户的 changes 只允许在末尾追加,历史不可改写。

Schema:
{
  "schemaVersion": 1,
  "accounts": [ {
    "id": "default", "currency": "CNY",
    "capital": 500000,            # 资金基数,不随行情变
    "horizonFundMonths": 12,      # 资金可用期限
    "maxSingleRatio": 0.25,       # 单一标的上限 (结构红线输入)
    "changes": [ { "ts": "...", "amount": 50000, "reason": "增资" } ]
  } ]
}
"""
from __future__ import annotations

import json
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


def write_accounts(data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """校验并落盘账户。校验失败抛 ValueError (调用方转 HTTP 400)。

    - capital >= 0
    - 0 < maxSingleRatio <= 1
    - horizonFundMonths > 0
    - changes 只允许追加: 传入 changes 不得少于现有,且现有 changes 必须是新 changes 的前缀
    """
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("accounts 必须是非空数组")
    for acc in accounts:
        _validate_account(acc)

    existing = read_accounts(data_dir)
    _enforce_changes_append_only(existing, accounts)

    out: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "accounts": accounts}
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
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


def _enforce_changes_append_only(existing: dict[str, Any], accounts: list[dict[str, Any]]) -> None:
    """现有账户的 changes 不得被删改,只能在末尾追加。新账户无约束。"""
    by_id = {a.get("id"): a for a in existing.get("accounts", [])}
    for acc in accounts:
        aid = acc.get("id")
        old = by_id.get(aid)
        if not old:
            continue
        old_changes = old.get("changes") or []
        new_changes = acc.get("changes") or []
        if len(new_changes) < len(old_changes):
            raise ValueError(f"账户 {aid}: changes 只允许追加,传入条数少于现有 {len(old_changes)} 条")
        # 现有 changes 必须逐条等于新 changes 的前缀 (历史不可改写)
        for i, oc in enumerate(old_changes):
            if i >= len(new_changes) or new_changes[i] != oc:
                raise ValueError(f"账户 {aid}: changes 历史记录不可改写,只能在末尾追加")
