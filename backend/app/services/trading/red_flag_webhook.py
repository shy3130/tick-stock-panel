"""纪律红旗 Webhook 推送。

配置环境变量 ``TRADING_RED_FLAG_WEBHOOK_URL`` 后,每个新红旗只推送一次。
Webhook 失败只记 warning,不影响交易事件/审计落盘；去重记录保存在
``data/user_data/trading/red_flag_webhook_sent.jsonl``。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import httpx

from app.services.trading import store

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _sent_path(data_dir: Path) -> Path:
    return store.trading_dir(data_dir) / "red_flag_webhook_sent.jsonl"


def _flag_key(trade_id: str, flag: dict[str, Any]) -> str:
    parts = (
        trade_id,
        str(flag.get("type") or ""),
        str(flag.get("ts") or ""),
        str(flag.get("kind") or ""),
        str(flag.get("old") or ""),
        str(flag.get("new") or ""),
        str(flag.get("price") or ""),
    )
    return "|".join(parts)


def _sent_keys(data_dir: Path) -> set[str]:
    p = _sent_path(data_dir)
    if not p.exists():
        return set()
    keys: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("key"):
            keys.add(str(row["key"]))
    return keys


def push_new_flags(
    data_dir: Path,
    trade_id: str,
    flags: list[dict[str, Any]],
    webhook_url: str | None = None,
) -> int:
    """推送未发送过的红旗,返回成功条数。无 URL / 失败均 fail-soft。"""
    url = str(webhook_url or os.getenv("TRADING_RED_FLAG_WEBHOOK_URL") or "").strip()
    if not url or not flags:
        return 0
    trade = store.read_trade(data_dir, trade_id) or {}
    symbol = trade.get("symbol")
    sent = _sent_keys(data_dir)
    success = 0
    for flag in flags:
        key = _flag_key(trade_id, flag)
        if key in sent:
            continue
        payload = {
            "ts": flag.get("ts"),
            "trade_id": trade_id,
            "symbol": symbol,
            "type": flag.get("type"),
            "message": _message(flag),
            "flag": flag,
        }
        try:
            httpx.post(url, json=payload, timeout=3.0, trust_env=False).raise_for_status()
        except Exception as exc:  # noqa: BLE001 — 推送不能阻断交易
            logger.warning("纪律红旗 webhook 推送失败: trade=%s type=%s error=%s", trade_id, flag.get("type"), exc)
            continue
        row = {"key": key, "tradeId": trade_id, "type": flag.get("type"), "ts": flag.get("ts")}
        with _lock:
            with _sent_path(data_dir).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        sent.add(key)
        success += 1
    return success


def _message(flag: dict[str, Any]) -> str:
    labels = {
        "stop_loss_widened": "放宽止损",
        "loss_add": "亏损加仓",
        "gate_bypassed": "绕过门禁",
        "audit_missing": "审计断链",
        "horizon_exceeded": "持仓超期",
        "size_over_limit": "仓位超限",
        "gate_proliferation": "门禁膨胀",
    }
    return labels.get(str(flag.get("type") or ""), "纪律红旗")
