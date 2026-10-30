"""日K异常及待重算状态。与一次自检报告分离, 缩小检查范围不能解除旧异常。

同一记录只有在日K校验及依赖重算均完成后才移除; 重启/取消/实时刷新不会绕过它。
读取仅 stat + 小 JSON 缓存, 不在策略或行情热路径扫描 Parquet。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import date
from functools import lru_cache
from pathlib import Path

from app.enriched_generation import EnrichedGenerationUnavailableError
from app.services.fs_utils import atomic_write_text

ASSET_LABELS = {"stock": "股票", "etf": "ETF", "index": "指数"}
_LOCK = threading.RLock()


class DailyIntegrityError(EnrichedGenerationUnavailableError):
    """已知原始日K异常或其依赖重算未完成, 与暂时正在发布区分。"""


def _path(root: Path) -> Path:
    return Path(root) / "user_data" / "daily_integrity_state.json"


@lru_cache(maxsize=32)
def _read(path: str, mtime: int, size: int) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value["version"] != 1 or not isinstance(value["entries"], list):
            raise ValueError("unsupported integrity state")
        for entry in value["entries"]:
            if entry["asset_type"] not in ASSET_LABELS:
                raise ValueError("invalid asset")
            date.fromisoformat(entry["day"])
            if entry["symbol"] is not None and not isinstance(entry["symbol"], str):
                raise ValueError("invalid symbol")
            if (not isinstance(entry["needs_raw"], bool)
                    or not isinstance(entry.get("needs_enriched", True), bool)
                    or entry["kind"] not in {"missing", "snapshot", "corrupt", "invalid"}
                    or not isinstance(entry["reason"], str)):
                raise ValueError("invalid repair state")
        revisions = value.get("revisions", {})
        if not isinstance(revisions, dict) or any(not isinstance(v, str) for v in revisions.values()):
            raise ValueError("invalid revision")
        return value
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DailyIntegrityError("日K完整性状态无法读取, 请检查数据目录") from exc


def snapshot(root: Path) -> dict:
    path = _path(root)
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"version": 1, "entries": [], "revisions": {}}
    except OSError as exc:
        raise DailyIntegrityError("日K完整性状态无法读取, 请检查数据目录权限") from exc
    return _read(str(path), info.st_mtime_ns, info.st_size)


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


def record_scan(root: Path, issues, *, assets: list[str], start: date, end: date) -> None:
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


def assert_usable(
    root: Path, asset_type: str = "stock", *, symbols: list[str] | None = None, end: date | None = None,
    enriched_only: bool = False,
) -> None:
    end = end or date.max
    for entry in snapshot(root)["entries"]:
        if enriched_only and not entry["needs_raw"] and not entry.get("needs_enriched", True):
            continue
        dependency = entry["asset_type"] == "index" and asset_type in {"stock", "etf"}
        if ((entry["asset_type"] == asset_type or dependency) and entry["day"] <= end.isoformat()
                and (dependency or symbols is None or entry["symbol"] is None or entry["symbol"] in symbols)):
            raise DailyIntegrityError(
                f"{ASSET_LABELS[entry['asset_type']]}日K在 {entry['day']} 有未完成修补或重算, "
                "请到 数据→自检/修补 完成后再计算"
            )
