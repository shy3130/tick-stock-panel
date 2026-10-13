"""进行中任务注册表 — 长任务(回测/挖掘/优化/滚动验证)跨设备接回。

前端在启动任务时 fire-and-forget 上报 (status=running, id=kind+参数指纹),
终态时按同一 id 关闭; 其他设备打开对应页面时, 本地无 reconnect 键则从注册表
取最近 running 项回填本地键, 复用既有 tryReconnect 机制 — 任务执行与重连
逻辑零改动, 上报失败也只是退化为原来的单机行为。

文件: data/user_data/task_registry.json
  {"items": [{id, kind, label, payload, status, started_at, updated_at}, ...]}

容量与时效: ≤20 条 (超限裁 updated_at 最旧); running 超过 24h 视为孤儿
(进程重启遗留), 读取时静默剔除并落盘回收。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

TASK_KINDS = frozenset({"backtest", "mining", "optimizer", "walkforward"})
TASK_STATUSES = frozenset({"running", "done", "error"})
MAX_ITEMS = 20
ORPHAN_RUNNING_SECONDS = 24 * 3600
_FILENAME = "task_registry.json"

_cache: dict[str, Any] | None = None
_cache_sig: tuple[int, int] | None = None


def _path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / _FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _signature(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _sanitize_items(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("kind") not in TASK_KINDS or it.get("status") not in TASK_STATUSES:
            continue
        if not isinstance(it.get("id"), str) or not it["id"]:
            continue
        out.append({
            "id": it["id"],
            "kind": it["kind"],
            "status": it["status"],
            "label": it["label"] if isinstance(it.get("label"), str) else "",
            "payload": it["payload"] if isinstance(it.get("payload"), dict) else {},
            "started_at": it.get("started_at") or 0,
            "updated_at": it.get("updated_at") or 0,
        })
    return out


def _sorted_trimmed(items: list[dict]) -> list[dict]:
    ordered = sorted(items, key=lambda x: x.get("updated_at") or 0, reverse=True)
    return ordered[:MAX_ITEMS]


def _prune_orphans(items: list[dict]) -> tuple[list[dict], int]:
    now = time.time()
    kept: list[dict] = []
    orphans = 0
    for it in items:
        if (
            it["status"] == "running"
            and it["updated_at"]
            and now - it["updated_at"] > ORPHAN_RUNNING_SECONDS
        ):
            orphans += 1
            continue
        kept.append(it)
    return kept, orphans


def _load_raw(data_dir: Path) -> list[dict] | None:
    global _cache, _cache_sig
    p = _path(data_dir)
    sig = _signature(p)
    if sig is None:
        return []
    if _cache is not None and sig == _cache_sig:
        return _cache["items"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — 损坏文件视为空注册表
        logger.warning("task registry 文件损坏, 视为空: %s", e)
        return []
    items = _sorted_trimmed(_sanitize_items(data))
    _cache, _cache_sig = {"items": items}, sig
    return items


def _save(data_dir: Path, items: list[dict]) -> None:
    global _cache, _cache_sig
    trimmed = _sorted_trimmed(items)
    p = _path(data_dir)
    atomic_write_text(p, json.dumps({"items": trimmed}, ensure_ascii=False))
    _cache, _cache_sig = {"items": trimmed}, _signature(p)


def list_items(data_dir: Path, kind: str | None = None) -> list[dict]:
    """按 updated_at 倒序返回 (可选 kind 过滤); 顺带回收孤儿 running 项。"""
    items = _load_raw(data_dir)
    if items is None:
        return []
    kept, orphans = _prune_orphans(items)
    if orphans:
        _save(data_dir, kept)
        items = kept
    if kind is None:
        return items
    return [it for it in items if it["kind"] == kind]


def upsert(
    data_dir: Path,
    task_id: str,
    *,
    kind: str,
    status: str,
    label: str = "",
    payload: dict | None = None,
) -> dict:
    if kind not in TASK_KINDS:
        raise ValueError(f"未知的任务类型: {kind}")
    if status not in TASK_STATUSES:
        raise ValueError(f"未知的任务状态: {status}")
    items = _load_raw(data_dir) or []
    now = time.time()
    entry = {
        "id": task_id,
        "kind": kind,
        "status": status,
        "label": label,
        "payload": payload or {},
        "started_at": now,
        "updated_at": now,
    }
    for i, it in enumerate(items):
        if it["id"] == task_id:
            entry["started_at"] = it.get("started_at") or now
            items[i] = entry
            break
    else:
        items.append(entry)
    _save(data_dir, items)
    return entry


def delete(data_dir: Path, task_id: str) -> bool:
    items = _load_raw(data_dir) or []
    remaining = [it for it in items if it["id"] != task_id]
    if len(remaining) == len(items):
        return False
    _save(data_dir, remaining)
    return True
