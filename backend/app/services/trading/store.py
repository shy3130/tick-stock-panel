"""Trading 持久化 — 单笔文件 + 两条 append-only 事件流。

存储布局 (data_dir/user_data/trading/):
- trades/{trade_id}.json   单笔当前事实(缓存投影,可被服务端更新)
- trade_events.jsonl       全部交易事件(唯一历史源,只追加)
- decision_audit.jsonl     决策审计(拦截/放行均留痕,只追加,永不清理)

审计与告警 (alerts.jsonl) 的关键区别: 不滚动清理 —— 审计断链本身就是红旗。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def trading_dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "trading"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trades_dir(data_dir: Path) -> Path:
    d = trading_dir(data_dir) / "trades"
    d.mkdir(parents=True, exist_ok=True)
    return d


def events_path(data_dir: Path) -> Path:
    return trading_dir(data_dir) / "trade_events.jsonl"


def audit_path(data_dir: Path) -> Path:
    return trading_dir(data_dir) / "decision_audit.jsonl"


def _trade_path(data_dir: Path, trade_id: str) -> Path:
    # trade_id 由服务端生成 (symbol_date_seq),仍做一次路径穿越防御
    safe = trade_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return _trades_dir(data_dir) / f"{safe}.json"


# ── 单笔当前事实 ─────────────────────────────────────────
def read_trade(data_dir: Path, trade_id: str) -> dict[str, Any] | None:
    p = _trade_path(data_dir, trade_id)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else None


def write_trade(data_dir: Path, trade: dict[str, Any]) -> None:
    p = _trade_path(data_dir, trade["tradeId"])
    p.write_text(json.dumps(trade, ensure_ascii=False, indent=2), encoding="utf-8")


def list_trades(data_dir: Path, status: str | None = None) -> list[dict[str, Any]]:
    """读取全部单笔 (按创建时间倒序)。损坏的文件被跳过。"""
    out: list[dict[str, Any]] = []
    for p in sorted(_trades_dir(data_dir).glob("*.json")):
        try:
            trade = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status and trade.get("status") != status:
            continue
        out.append(trade)
    out.sort(key=lambda t: t.get("createdAt", ""), reverse=True)
    return out


def next_trade_seq(data_dir: Path, symbol: str, day: str) -> int:
    """同标的同日的下一个序号 (trade_id = {symbol}_{yyyymmdd}_{seq})。"""
    prefix = f"{symbol}_{day}_"
    n = 0
    for p in _trades_dir(data_dir).glob(f"{prefix}*.json"):
        suffix = p.stem[len(prefix):]
        if suffix.isdigit():
            n = max(n, int(suffix))
    return n + 1


# ── append-only 事件流 / 审计流 ──────────────────────────
def append_event(data_dir: Path, event: dict[str, Any]) -> None:
    """追加一条交易事件。写失败抛 OSError (调用方必须显式处理,不得静默)。"""
    with _lock:
        with events_path(data_dir).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_audit(data_dir: Path, entry: dict[str, Any]) -> None:
    """追加一条决策审计。写失败抛 OSError —— 审计断链必须显式暴露。"""
    with _lock:
        with audit_path(data_dir).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_events(data_dir: Path, trade_id: str | None = None) -> list[dict[str, Any]]:
    events = _read_jsonl(events_path(data_dir))
    if trade_id is not None:
        events = [e for e in events if e.get("tradeId") == trade_id]
    return events


def read_audit(
    data_dir: Path,
    trade_id: str | None = None,
    passed: bool | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    entries = _read_jsonl(audit_path(data_dir))
    if trade_id is not None:
        entries = [e for e in entries if e.get("tradeId") == trade_id]
    if passed is not None:
        entries = [e for e in entries if e.get("passed") is passed]
    return entries[-limit:][::-1]  # 时间倒序


def persist_trade_with_event(data_dir: Path, trade: dict[str, Any], event: dict[str, Any]) -> None:
    """单笔投影 + 事件流在同一临界区落盘,避免投影与历史分叉。"""
    with _lock:
        write_trade(data_dir, trade)
        with events_path(data_dir).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
