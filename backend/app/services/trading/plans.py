"""交易计划台 — data/user_data/trading/plans/{yyyymmdd}.json。

盘前为每笔候选/持仓写入当日计划 (动作、触发条件、数量、理由),
盘中执行, 盘后对比当日事件流得到计划 vs 实际偏差。

Schema:
{
  "schemaVersion": 1,
  "date": "20260804",
  "entries": [ {
    "id": "...", "symbol": "...", "tradeId": null,
    "action": "buy_new|add|tp|sl|close|watch",
    "trigger": "...", "qty": null, "reason": "...", "createdAt": "..."
  } ],
  "actualNotes": ""
}

read_plan/write_plan: 覆盖当日; entries 有 id 则更新无则追加。
deviation: 对比计划 entries 与 trade_events.jsonl 当日事件, 三分类输出。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.trading import store

SCHEMA_VERSION = 1

_lock = threading.Lock()

# plan action → 匹配的事件 kind 集合 (多对一)
# buy_new 对应 open/fill; add/tp/sl/close 同名; watch 无对应事件 (纯观察)
_ACTION_TO_KINDS: dict[str, set[str]] = {
    "buy_new": {"open", "fill"},
    "add": {"add"},
    "tp": {"tp"},
    "sl": {"sl"},
    "close": {"close"},
    "adjust": {"adjust"},
}

# 事件 kind → 反查的 plan action 集合
_KIND_TO_ACTIONS: dict[str, set[str]] = {}
for _act, _kinds in _ACTION_TO_KINDS.items():
    for _k in _kinds:
        _KIND_TO_ACTIONS.setdefault(_k, set()).add(_act)

_PLAN_ACTIONS = set(_ACTION_TO_KINDS) | {"watch"}


# ── 路径 ─────────────────────────────────────────────────
def _plans_dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "trading" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plan_path(data_dir: Path, date: str) -> Path:
    return _plans_dir(data_dir) / f"{date}.json"


# ── CRUD ─────────────────────────────────────────────────
def read_plan(data_dir: Path, date: str) -> dict[str, Any] | None:
    """读取当日计划。文件不存在/损坏 → None。"""
    p = _plan_path(data_dir, date)
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
    if not isinstance(data, dict):
        return None
    return data


def write_plan(data_dir: Path, date: str, payload: dict[str, Any]) -> dict[str, Any]:
    """写入当日计划。

    默认是增量合并：entries 同 id 更新、未提交的既有条目保留。
    payload.replace=true 时把 entries 视为完整当日清单并全量替换，
    供前端删除计划条目；交易计划不是 append-only 事实源。
    校验失败抛 ValueError (调用方转 HTTP 400)。
    """
    if not isinstance(payload, dict):
        raise ValueError("计划必须是对象")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("entries 必须是数组")
    now = _now_str()
    existing = read_plan(data_dir, date)
    existing_entries = {e["id"]: e for e in (existing.get("entries") or []) if isinstance(e, dict) and e.get("id")} if existing else {}

    out_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("计划条目必须是对象")
        entry = _validate_entry(raw, now)
        eid = entry["id"]
        if eid in seen_ids:
            raise ValueError(f"重复的计划条目 id: {eid}")
        seen_ids.add(eid)
        if eid in existing_entries:
            old = existing_entries[eid]
            if not entry.get("createdAt") and old.get("createdAt"):
                entry["createdAt"] = old["createdAt"]
        out_entries.append(entry)

    # 默认保留未提交的既有条目(增量追加)；replace=true 时全量替换以支持删除。
    if not payload.get("replace"):
        for old_id, old in existing_entries.items():
            if old_id not in seen_ids:
                out_entries.append(old)
                seen_ids.add(old_id)

    out: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "date": date,
        "entries": out_entries,
        "actualNotes": str(payload.get("actualNotes") or (existing.get("actualNotes") if existing else "") or ""),
    }
    p = _plan_path(data_dir, date)
    tmp = p.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    return out


def _validate_entry(raw: dict[str, Any], now: str) -> dict[str, Any]:
    eid = str(raw.get("id") or "").strip()
    if not eid:
        raise ValueError("计划条目 id 必填")
    symbol = str(raw.get("symbol") or "").strip()
    if not symbol:
        raise ValueError(f"计划条目 {eid}: symbol 必填")
    action = str(raw.get("action") or "").strip()
    if action not in _PLAN_ACTIONS:
        raise ValueError(f"计划条目 {eid}: action 必须是 {'/'.join(sorted(_PLAN_ACTIONS))}")
    entry: dict[str, Any] = {
        "id": eid,
        "symbol": symbol,
        "tradeId": (str(raw.get("tradeId")).strip() or None) if raw.get("tradeId") else None,
        "action": action,
        "trigger": str(raw.get("trigger") or "").strip(),
        "reason": str(raw.get("reason") or "").strip(),
        "createdAt": str(raw.get("createdAt") or now).strip() or now,
    }
    qty = raw.get("qty")
    if qty is not None and not isinstance(qty, bool):
        try:
            fq = float(qty)
            entry["qty"] = fq if fq > 0 else None
        except (TypeError, ValueError):
            entry["qty"] = None
    else:
        entry["qty"] = None
    return entry


# ── 偏差分析 ─────────────────────────────────────────────
def deviation(data_dir: Path, date: str) -> dict[str, Any]:
    """对比当日计划 entries 与 trade_events.jsonl 中 ts 属于该日的事件。

    匹配键: symbol + 动作类别 (buy_new↔open/fill, add/tp/sl/close 同名)。
    返回 {planned_but_not_done, done_but_not_planned, matched}。
    """
    plan = read_plan(data_dir, date)
    plan_entries = (plan.get("entries") or []) if plan else []
    events = _read_events_for_date(data_dir, date)

    planned: list[dict[str, Any]] = []
    for e in plan_entries:
        if not isinstance(e, dict):
            continue
        action = e.get("action")
        if action == "watch":
            continue  # watch 是纯观察, 不算未执行
        symbol = str(e.get("symbol") or "").strip()
        planned.append({
            "key": _match_key(symbol, action),
            "id": e.get("id"),
            "symbol": symbol,
            "action": action,
            "tradeId": e.get("tradeId"),
        })

    done: list[dict[str, Any]] = []
    for ev in events:
        kind = ev.get("kind")
        if not kind or kind in ("prepare", "revise"):
            continue
        trade_id = ev.get("tradeId")
        symbol = _event_symbol(ev)
        if not symbol:
            continue
        actions = _KIND_TO_ACTIONS.get(kind, set())
        done.append({
            "key": _match_key(symbol, actions),
            "symbol": symbol,
            "kind": kind,
            "tradeId": trade_id,
            "ts": ev.get("ts"),
        })

    done_keys = {d["key"] for d in done}
    planned_keys = {p["key"] for p in planned}

    matched = [p for p in planned if p["key"] in done_keys]
    planned_but_not_done = [p for p in planned if p["key"] not in done_keys]
    done_but_not_planned = [d for d in done if d["key"] not in planned_keys]

    return {
        "date": date,
        "plannedCount": len(planned),
        "doneCount": len(done),
        "planned_but_not_done": planned_but_not_done,
        "done_but_not_planned": done_but_not_planned,
        "matched": matched,
    }


# ── 工具 ─────────────────────────────────────────────────
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _match_key(symbol: str, action: Any) -> frozenset[str]:
    """symbol 大小写无关 + action 可为单值或集合。返回可哈希的匹配键。"""
    actions = action if isinstance(action, set) else {action}
    return frozenset({symbol.upper(), *actions})


def _event_symbol(ev: dict[str, Any]) -> str:
    """事件可能带 symbol (payload), 否则查 trade 文件。优先 payload.symbol。"""
    payload = ev.get("payload")
    if isinstance(payload, dict):
        s = str(payload.get("symbol") or "").strip()
        if s:
            return s
    return str(ev.get("symbol") or "").strip()


def _read_events_for_date(data_dir: Path, date: str) -> list[dict[str, Any]]:
    """读取 ts 属于 date (yyyymmdd → YYYY-MM-DD) 的全部交易事件, 补 symbol。"""
    all_events = store.read_events(data_dir, trade_id=None)
    target = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date
    trades = {t["tradeId"]: t for t in store.list_trades(data_dir) if t.get("tradeId")}
    out: list[dict[str, Any]] = []
    for ev in all_events:
        ts = str(ev.get("ts") or "")
        if ts.startswith(target):
            ev = dict(ev)
            if not ev.get("symbol"):
                trade = trades.get(ev.get("tradeId"))
                if trade:
                    ev["symbol"] = trade.get("symbol")
            out.append(ev)
    return out
