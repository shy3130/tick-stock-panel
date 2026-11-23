"""日K异常及待重算状态。与一次自检报告分离, 缩小检查范围不能解除旧异常。

同一记录只有在日K校验及依赖重算均完成后才移除; 重启/取消/实时刷新不会绕过它。
读取仅 stat + 小 JSON 缓存, 不在策略或行情热路径扫描 Parquet。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import date
from pathlib import Path

from tivelo_data.legacy.repository_state import (
    ASSET_LABELS as ASSET_LABELS,
)
from tivelo_data.legacy.repository_state import (
    DailyIntegrityError as DailyIntegrityError,
)
from tivelo_data.legacy.repository_state import (
    _path,
    snapshot,
)
from tivelo_data.legacy.repository_state import (
    _read as _read,
)
from tivelo_data.legacy.repository_state import (
    assert_usable as assert_usable,
)

from app.services.fs_utils import atomic_write_text

_LOCK = threading.RLock()


def revision(root: Path, asset_type: str = "stock") -> str:
    revisions = snapshot(root).get("revisions", {})
    own = revisions.get(asset_type, "0")
    benchmark = revisions.get("index", "0") if asset_type in {"stock", "etf"} else "0"
    return f"{own}:{benchmark}" if benchmark != "0" else own


def _key(entry: dict):
    return entry["asset_type"], entry["day"], entry["symbol"]


def _save(root: Path, old: dict, entries: list[dict]) -> None:
    entries.sort(key=lambda e: (e["asset_type"], e["day"], e["symbol"] or ""))
    if entries == old["entries"]:
        return
    revisions = dict(old.get("revisions", {}))
    for asset in ASSET_LABELS:
        if ([e for e in entries if e["asset_type"] == asset]
                != [e for e in old["entries"] if e["asset_type"] == asset]):
            revisions[asset] = uuid.uuid4().hex
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"version": 1, "entries": entries, "revisions": revisions}, ensure_ascii=False))


def _issue_entries(issues) -> dict:
    from app.services.data_integrity import TABLE_FAMILY

    found = {}
    for issue in issues:
        if issue.kind == "unconfirmed":
            continue
        for symbol in issue.symbols or (None,):
            entry = {"asset_type": TABLE_FAMILY[issue.table], "day": issue.day.isoformat(),
                     "symbol": symbol, "needs_raw": True, "needs_enriched": True,
                     "kind": issue.kind, "reason": issue.reason}
            found[_key(entry)] = entry
    return found


def record_issues(root: Path, issues) -> None:
    """登记计算中新发现的问题, 不把未检查的日期/股票标记为已校验。"""
    found = _issue_entries(issues)
    if not found:
        return
    with _LOCK:
        old = snapshot(root)
        entries = [e for e in old["entries"] if _key(e) not in found]
        _save(root, old, [*entries, *found.values()])


def record_scan(root: Path, issues, *, assets: list[str], start: date, end: date) -> None:
    found = _issue_entries(issues)
    with _LOCK:
        old = snapshot(root)
        entries = []
        for entry in old["entries"]:
            key = _key(entry)
            if key in found:
                continue
            entry = dict(entry)
            if entry["asset_type"] in assets and start.isoformat() <= entry["day"] <= end.isoformat():
                entry["needs_raw"] = False
            entries.append(entry)
        entries.extend(found.values())
        _save(root, old, entries)


def complete(root: Path, asset_type: str, *, start: date, symbols: list[str] | None,
             days: set[date] | None = None) -> None:
    selected_days = {d.isoformat() for d in days} if days is not None else None
    with _LOCK:
        old = snapshot(root)
        entries = [e for e in old["entries"] if not (
            e["asset_type"] == asset_type and e["day"] >= start.isoformat()
            and (selected_days is None or e["day"] in selected_days)
            and (symbols is None or e["symbol"] in symbols)
        )]
        _save(root, old, entries)


def mark_enriched_ready(root: Path, asset_type: str, *, start: date, symbols: list[str] | None,
                        days: set[date] | None = None) -> None:
    """基础指标已经发布; 共享市场统计可读取它们, 对外计算仍等整个修补完成。"""
    selected_days = {d.isoformat() for d in days} if days is not None else None
    with _LOCK:
        old = snapshot(root)
        entries = []
        for item in old["entries"]:
            entry = dict(item)
            if (entry["asset_type"] == asset_type and entry["day"] >= start.isoformat()
                    and (selected_days is None or entry["day"] in selected_days)
                    and (symbols is None or entry["symbol"] in symbols)):
                entry.update(needs_raw=False, needs_enriched=False)
            entries.append(entry)
        _save(root, old, entries)


def clear_assets(root: Path, assets: set[str]) -> None:
    """仅随明确的清除数据操作清理对应状态。"""
    with _LOCK:
        if assets == set(ASSET_LABELS):
            path = _path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps({"version": 1, "entries": [],
                "revisions": {asset: uuid.uuid4().hex for asset in assets}}))
            return
        old = snapshot(root)
        _save(root, old, [e for e in old["entries"] if e["asset_type"] not in assets])
