"""fhold 持仓接入 — 从 fhold-cli (../fhold) 读取真实券商持仓。

fhold 是独立的持仓管理工具 (CLI + SQLite/HTTP 后端),本模块只做只读接入:
- 通过 `fhold-cli --format json` 拉取账户与持仓
- fail-soft: CLI 不存在 / 超时 / 输出非法 → available=False,绝不抛错阻断快照

代码 → 内部 symbol 映射:
- 6xxxxx → .SH | 0/3xxxxx → .SZ | 4/8xxxxx → .BJ | 5 位数字 → .HK
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _cli() -> str | None:
    path = os.environ.get("FHOLD_CLI", "fhold-cli")
    return path if shutil.which(path) else None


def _run(args: list[str]) -> Any | None:
    cli = _cli()
    if not cli:
        return None
    try:
        proc = subprocess.run(
            [cli, *args, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("fhold-cli 调用失败: %s", e)
        return None
    if proc.returncode != 0:
        logger.warning("fhold-cli 退出码 %s: %s", proc.returncode, proc.stderr.strip()[:200])
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("fhold-cli 输出非 JSON: %s", proc.stdout.strip()[:200])
        return None


def to_symbol(code: str) -> str | None:
    """fhold 股票代码 → 内部 symbol (带市场后缀)。无法识别返回 None。"""
    c = str(code or "").strip()
    if not c.isdigit():
        return None
    if len(c) == 5:
        return f"{c}.HK"
    if len(c) != 6:
        return None
    if c.startswith("6"):
        return f"{c}.SH"
    if c.startswith(("0", "3")):
        return f"{c}.SZ"
    if c.startswith(("4", "8")):
        return f"{c}.BJ"
    return None


def fetch_holdings() -> dict[str, Any]:
    """拉取 fhold 账户与持仓。任何失败都返回 available=False。"""
    accounts_raw = _run(["account", "list"])
    positions_raw = _run(["position", "list"])
    if accounts_raw is None and positions_raw is None:
        return {"available": False, "accounts": [], "positions": []}

    accounts = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "broker": a.get("broker"),
            "isDefault": bool(a.get("is_default")),
        }
        for a in (accounts_raw or [])
        if isinstance(a, dict) and a.get("is_active", True)
    ]
    positions = []
    for p in positions_raw or []:
        if not isinstance(p, dict):
            continue
        code = str(p.get("code") or "")
        positions.append({
            "symbol": to_symbol(code),
            "code": code,
            "name": p.get("name"),
            "accountId": p.get("account_id"),
            "qty": p.get("quantity"),
            "costPrice": p.get("cost_price"),
            "currentPrice": p.get("current_price"),
            "marketValue": p.get("amount"),
            "holdingPnl": p.get("holding_pnl"),
            "holdingPnlRatio": p.get("holding_pnl_ratio"),
            "sourceDate": p.get("source_date"),
            "updatedAt": p.get("updated_at"),
        })
    return {"available": True, "accounts": accounts, "positions": positions}
