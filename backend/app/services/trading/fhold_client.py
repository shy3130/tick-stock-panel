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
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0

_ETF_CODE_MAP: dict[str, str] | None = None


def _workspace_cli() -> str | None:
    """优先复用当前开发工作区同级 fhold 的新构建 CLI。"""
    candidate = Path(__file__).resolve().parents[5] / "fhold" / "fhold-cli"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _cli() -> str | None:
    """解析显式覆盖、同级开发二进制和 PATH 中的 CLI。"""
    if "FHOLD_CLI" in os.environ:
        path = os.environ["FHOLD_CLI"]
        return path if shutil.which(path) else None
    return _workspace_cli() or shutil.which("fhold-cli")


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
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("fhold-cli 调用失败 (%s)", type(exc).__name__)
        return None
    if proc.returncode != 0:
        logger.warning(
            "fhold-cli 退出码 %s (stderr_bytes=%s)", proc.returncode, len(proc.stderr.encode())
        )
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.warning(
            "fhold-cli 输出非 JSON (stdout_bytes=%s, line=%s, column=%s)",
            len(proc.stdout.encode()),
            exc.lineno,
            exc.colno,
        )
        return None


def _etf_code_map() -> dict[str, str]:
    """本地 ETF universe 的 code → symbol 映射(懒加载, 失败即空表)。"""
    global _ETF_CODE_MAP
    if _ETF_CODE_MAP is None:
        mapping: dict[str, str] = {}
        try:
            import polars as pl

            from app.config import settings

            path = Path(settings.data_dir) / "instruments_etf" / "instruments_etf.parquet"
            if path.is_file():
                df = pl.read_parquet(path, columns=["symbol", "code"])
                mapping = {
                    str(row["code"]): str(row["symbol"])
                    for row in df.iter_rows(named=True)
                    if str(row.get("code") or "").strip()
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("ETF 代码映射加载失败: %s", type(exc).__name__)
        _ETF_CODE_MAP = mapping
    return _ETF_CODE_MAP


def to_symbol(code: str) -> str | None:
    """fhold 证券代码 → 内部 symbol (带市场后缀)。无法识别返回 None。

    股票按前缀判定; 1/5 开头的基金与 ETF 沪深后缀无法从前缀区分
    (11x 沪可转债 vs 12x 深可转债), 统一经本地 ETF universe code 精确解析。
    """
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
    return _etf_code_map().get(c)


def _normalize_accounts(raw: Any | None) -> list[dict[str, Any]]:
    return [
        {
            "id": account.get("id"),
            "name": account.get("name"),
            "broker": account.get("broker"),
            "isDefault": bool(account.get("is_default")),
        }
        for account in (raw or [])
        if isinstance(account, dict) and account.get("is_active", True)
    ]


def _transaction_snapshot(
    payload: Any | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """验证 fhold CLI 证明为本地一致读事务的完整交易与账户快照。"""
    if not isinstance(payload, dict):
        logger.warning("fhold-cli 交易快照缺少完整性元数据")
        return None
    items = payload.get("items")
    accounts = payload.get("accounts")
    total = payload.get("total")
    count = payload.get("count")
    if (
        payload.get("consistent") is not True
        or payload.get("mode") != "local"
        or not isinstance(items, list)
        or not isinstance(accounts, list)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or total < 0
        or count != total
        or len(items) != total
        or not all(isinstance(item, dict) for item in items)
        or not all(isinstance(account, dict) for account in accounts)
    ):
        logger.warning("fhold-cli 交易快照无法证明完整一致性")
        return None
    rows = [dict(item) for item in items]
    transaction_ids = [str(row.get("id") or "").strip() for row in rows]
    if not all(transaction_ids) or len(set(transaction_ids)) != total:
        logger.warning("fhold-cli 交易快照包含空或重复交易 ID")
        return None
    return rows, [dict(account) for account in accounts]


def fetch_holdings() -> dict[str, Any]:
    """拉取 fhold 账户与持仓。任何失败都返回 available=False。"""
    accounts_raw = _run(["account", "list"])
    positions_raw = _run(["position", "list"])
    if accounts_raw is None and positions_raw is None:
        return {"available": False, "accounts": [], "positions": []}

    accounts = _normalize_accounts(accounts_raw)
    positions = []
    for p in positions_raw or []:
        if not isinstance(p, dict):
            continue
        code = str(p.get("code") or "")
        positions.append(
            {
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
            }
        )
    return {"available": True, "accounts": accounts, "positions": positions}


def fetch_transactions() -> dict[str, Any]:
    """拉取完整 fhold 成交流水；只接受 CLI 证明的一致本地快照。"""
    snapshot = _transaction_snapshot(_run(["tx", "snapshot"]))
    if snapshot is None:
        return {"available": False, "accounts": [], "transactions": []}
    transactions, accounts_raw = snapshot
    return {
        "available": True,
        "accounts": _normalize_accounts(accounts_raw),
        "transactions": transactions,
    }
