"""策略池与策略库元数据服务 — 池成员 + 自定义分组 + 档案库。

概念分工:
  - 策略池 (pool): 参与 scanning 的策略 ID 有序列表, 纯顺序语义, 不携带属性。
  - 策略库元数据 (meta): 按策略 ID 记录 **库级** 属性 —— 自定义分组、归档
    标记、加入时间。分组/归档属于「策略库里的这个策略」, 与它是否在池中
    无关; 「自定义/AI/叠加」是策略来源分类, 由策略自身决定, 不在此存储。

存储 ``data/user_data/strategy_pool.json``:
  {"version": 2, "groups": [{id, name}], "pool": ["id", ...],
   "meta": {"id": {"group": g|None, "archived": bool, "archived_at": str|None,
                   "added_at": str}}}

归档语义 (与因子/信号一致, 可逆、不删除策略文件):
  归档 = meta.archived=True 并从池中移出 (过期策略不再参与扫描);
  恢复 = 仅解除归档回到策略库, 不自动回到池 (由用户重新勾选)。

迁移链: 无文件 → 从 ui_prefs.strategy_pool (v1 纯 ID 数组) 种子;
  v1 文件 (entries/archived 结构) → 池取 entries 顺序, 属性折入 meta。
前端每次变更向旧偏好键影子写入纯 ID 数组, 保证降级回滚可用。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.services import preferences
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_MAX_GROUP_NAME_LENGTH = 24
_MAX_POOL = 500
_MAX_META = 2000


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path() -> Path:
    p = settings.data_dir / "user_data" / "strategy_pool.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _empty_state() -> dict:
    return {"version": 2, "revision": 0, "groups": [], "pool": [], "meta": {}}


def _normalize_meta(raw: object) -> dict | None:
    """单条 meta 归一: {group, archived, archived_at, added_at}; 非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    group = raw.get("group")
    return {
        "group": group if isinstance(group, str) and group else None,
        "archived": raw.get("archived") is True,
        "archived_at": str(raw["archived_at"]) if raw.get("archived_at") else None,
        "added_at": str(raw.get("added_at") or "") or None,
    }


def _normalize_state(raw: dict) -> dict:
    groups: list[dict] = []
    for item in raw.get("groups") or []:
        if isinstance(item, dict) and item.get("id") and item.get("name"):
            groups.append({"id": str(item["id"]), "name": str(item["name"])})
    group_ids = {g["id"] for g in groups}

    pool: list[str] = []
    for item in raw.get("pool") or []:
        if isinstance(item, str) and item and item not in pool:
            pool.append(item)

    meta: dict[str, dict] = {}
    for key, value in (raw.get("meta") or {}).items():
        if not isinstance(key, str) or not key:
            continue
        record = _normalize_meta(value)
        if record is None:
            continue
        if record["group"] not in group_ids:
            record["group"] = None  # 分组被手工删掉时兜底为未分组
        meta[key] = record
    revision = raw.get("revision", 0)
    return {"version": 2, "revision": revision, "groups": groups, "pool": pool, "meta": meta}


def validate_snapshot(raw: object) -> dict:
    """旧文件及备份共用解析; 更高版本或坏结构拒绝写入, 不静默清空配置。"""
    if not isinstance(raw, dict):
        raise ValueError("策略池数据必须是 JSON 对象")
    version = raw.get("version", 1 if "entries" in raw else 2)
    if type(version) is not int or version not in (1, 2):
        raise ValueError(f"不支持的策略池版本: {version!r}")
    if not isinstance(raw.get("groups", []), list):
        raise ValueError("策略分组必须是数组")
    revision = raw.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("策略池 revision 必须是非负整数")
    if version == 1:
        if not all(isinstance(raw.get(key, []), list) for key in ("entries", "archived")):
            raise ValueError("旧版策略池条目必须是数组")
        raw = {**_migrate_v1(raw), "revision": revision}
    if not isinstance(raw.get("pool", []), list) or not isinstance(raw.get("meta", {}), dict):
        raise ValueError("策略池 pool 必须是数组、meta 必须是对象")
    if any(not isinstance(sid, str) or not sid for sid in raw.get("pool", [])):
        raise ValueError("策略池包含非法策略 ID")
    for group in raw.get("groups", []):
        if not isinstance(group, dict) or not isinstance(group.get("id"), str) or not group["id"]:
            raise ValueError("策略分组缺少合法 ID")
        if not isinstance(group.get("name"), str) or not group["name"].strip():
            raise ValueError("策略分组缺少名称")
    if any(not isinstance(value, dict) for value in raw.get("meta", {}).values()):
        raise ValueError("策略库记录必须是对象")
    state = _normalize_state(raw)
    # 手工编辑或旧备份里同时存在归档标记与池条目时, 归档仍优先生效。
    state["pool"] = [sid for sid in state["pool"] if not state["meta"].get(sid, {}).get("archived")]
    return state


def _migrate_v1(raw: dict) -> dict:
    """v1 (entries/archived 各带属性) → v2 (pool 纯 ID + meta 库级属性)。"""
    groups: list[dict] = [
        {"id": str(g.get("id")), "name": str(g.get("name"))}
        for g in raw.get("groups") or []
        if isinstance(g, dict) and g.get("id") and g.get("name")
    ]
    group_ids = {g["id"] for g in groups}
    pool: list[str] = []
    meta: dict[str, dict] = {}
    for entry in raw.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        sid = str(entry["id"])
        if sid in meta:
            continue
        pool.append(sid)
        group = entry.get("group")
        meta[sid] = {
            "group": group if group in group_ids else None,
            "archived": False,
            "archived_at": None,
            "added_at": str(entry.get("added_at") or "") or None,
        }
    for entry in raw.get("archived") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        sid = str(entry["id"])
        if sid in meta:
            continue
        group = entry.get("group")
        meta[sid] = {
            "group": group if group in group_ids else None,
            "archived": True,
            "archived_at": str(entry.get("archived_at") or "") or None,
            "added_at": str(entry.get("added_at") or "") or None,
        }
    return {"version": 2, "groups": groups, "pool": pool, "meta": meta}


def _seed_pool_from_preferences() -> list[str]:
    """旧版 ui_prefs.strategy_pool (JSON 文本 ID 数组) → 池顺序。读不到返回空。"""
    try:
        raw = preferences.get_ui_prefs().get("strategy_pool")
        if not isinstance(raw, str):
            return []
        ids = json.loads(raw)
        if not isinstance(ids, list):
            return []
    except (ValueError, OSError):
        return []
    return list(dict.fromkeys(item for item in ids if isinstance(item, str) and item))


def _read() -> dict:
    p = _path()
    if not p.exists():
        # 首次访问: 从旧偏好一次性迁移 (写盘保证多客户端只迁一次)
        state = _empty_state()
        state["pool"] = _seed_pool_from_preferences()
        _write(state)
        if state["pool"]:
            logger.info("strategy pool migrated from ui_prefs: %d ids", len(state["pool"]))
        return state
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # 损坏时不静默重建 (fail-closed): 宁可让用户看到错误, 也不丢分组/档案
        raise ValueError("策略池数据损坏，请检查 user_data/strategy_pool.json") from exc
    state = validate_snapshot(raw)
    if isinstance(raw.get("entries"), list):  # v1 结构
        _write(state)
        logger.info("strategy pool migrated v1 -> v2")
    return state


def _write(state: dict) -> None:
    state["revision"] = state.get("revision", 0) + 1
    payload = {"version": 2, **{k: state[k] for k in ("revision", "groups", "pool", "meta")}}
    atomic_write_text(_path(), json.dumps(payload, ensure_ascii=False, indent=2))
    # 旧版只认识 ui_prefs.strategy_pool。服务端在同一把锁内维护影子, 避免
    # 多窗口把晚到的旧前端偏好写回, 降级时恢复出过时的池。
    try:
        preferences.set_ui_prefs({"strategy_pool": json.dumps(state["pool"], ensure_ascii=False)})
    except (OSError, ValueError):
        logger.warning("strategy pool saved, but legacy preference shadow could not be updated")


def load_snapshot() -> dict:
    """完整快照: {groups, pool, meta}。"""
    with _LOCK:
        return _read()


def library_metadata(data_dir: Path) -> dict[str, dict]:
    """列表一次性附加库级属性; 不初始化文件、不改变引擎或旧引用的执行语义。"""
    path = data_dir / "user_data" / "strategy_pool.json"
    with _LOCK:
        if not path.is_file():
            return {}
        state = validate_snapshot(json.loads(path.read_text(encoding="utf-8")))
    names = {group["id"]: group["name"] for group in state["groups"]}
    return {sid: {**meta, "group_name": names.get(meta["group"])} for sid, meta in state["meta"].items()}


def restore_snapshot(raw: object, *, allow_overwrite: bool = False) -> dict:
    """备份恢复复用写锁及版本保护。恢复旧快照仍产生新 revision。"""
    restored = validate_snapshot(raw)
    with _LOCK:
        exists = _path().exists()
        if exists and not allow_overwrite:
            raise FileExistsError("本地已有策略库配置，已保留原文件")
        current_revision = _read()["revision"] if exists else 0
        restored["revision"] = max(current_revision, restored["revision"])
        _write(restored)
        return restored


def restore_legacy_preferences(*, allow_overwrite: bool = False) -> None:
    """旧备份只有池 ID: 仅在允许覆盖时更新已有池, 不改变新分组/归档。

    先捕获已合并的旧偏好, 再读权威文件; 最后校准降级影子, 包内旧偏好不能
    越过新文件的冲突保护。新目录仍按原迁移路径从偏好建立文件。
    """
    with _LOCK:
        ids = _seed_pool_from_preferences()
        exists = _path().exists()
        state = _read()
        if exists and allow_overwrite:
            pool = [sid for sid in ids if not state["meta"].get(sid, {}).get("archived")]
            if pool != state["pool"]:
                state["pool"] = pool
                _write(state)
        preferences.set_ui_prefs({"strategy_pool": json.dumps(state["pool"], ensure_ascii=False)})


def _ensure_meta(state: dict, strategy_id: str) -> dict:
    record = state["meta"].get(strategy_id)
    if record is None:
        record = {"group": None, "archived": False, "archived_at": None, "added_at": _now()}
        state["meta"][strategy_id] = record
    return record


def register(strategy_id: str) -> None:
    """登记一个策略的加入时间 (策略保存/发布时调用; 已登记则幂等)。"""
    with _LOCK:
        state = _read()
        if strategy_id in state["meta"]:
            return
        _ensure_meta(state, strategy_id)
        _write(state)


def sync_known_ids(ids: list[str] | set[str]) -> dict:
    """把当前引擎里的全部策略 ID 登记进 meta (加入时间=首次见到)。

    惰性注册: 未登记的策略在被看到时补写 added_at, 让「加入时间排序」对
    新策略立即有效。已登记记录不动 (加入时间不漂移)。
    """
    with _LOCK:
        state = _read()
        changed = False
        for sid in ids:
            if isinstance(sid, str) and sid and sid not in state["meta"]:
                if len(state["meta"]) >= _MAX_META:
                    break
                _ensure_meta(state, sid)
                changed = True
        if changed:
            _write(state)
        return state


def _normalize_group_name(name: str) -> str:
    normalized = (name or "").strip()
    if not normalized:
        raise ValueError("分组名称不能为空")
    if len(normalized) > _MAX_GROUP_NAME_LENGTH:
        raise ValueError(f"分组名称过长（最多 {_MAX_GROUP_NAME_LENGTH} 字）")
    return normalized


def _validate_group_id(group_id: str | None, groups: list[dict]) -> None:
    if group_id is not None and not any(g["id"] == group_id for g in groups):
        raise ValueError("分组不存在")


# ── 池成员 (纯顺序语义) ─────────────────────────────────


def set_pool(ids: list[str], *, only_if_empty: bool = False, expected_revision: int | None = None) -> dict:
    """整体替换池 (顺序即池顺序); 重复 ID 拒绝。已归档策略拒绝入池。"""
    with _LOCK:
        state = _read()
        if only_if_empty and state["pool"]:
            return state  # 迁移期间另一窗口已填入, 不覆盖它的新选择
        if expected_revision is not None and state["revision"] != expected_revision:
            raise ValueError("策略池已在其他窗口更新，请刷新后重试")
        seen: set[str] = set()
        pool: list[str] = []
        for sid in ids:
            if not isinstance(sid, str) or not sid:
                raise ValueError(f"非法策略 ID: {sid!r}")
            if sid in seen:
                raise ValueError(f"策略池条目重复: {sid}")
            seen.add(sid)
            pool.append(sid)
        if len(pool) > _MAX_POOL:
            raise ValueError(f"策略池条目过多（最多 {_MAX_POOL} 条）")
        archived = {sid for sid, m in state["meta"].items() if m["archived"]}
        blocked = [sid for sid in pool if sid in archived]
        if blocked:
            raise ValueError(f"已归档策略不能入池，请先恢复: {', '.join(blocked)}")
        state["pool"] = pool
        _write(state)
        return state


def add(strategy_id: str) -> dict:
    with _LOCK:
        state = _read()
        if strategy_id in state["pool"]:
            return state
        if state["meta"].get(strategy_id, {}).get("archived"):
            raise ValueError("该策略已归档，请先在档案库恢复")
        state["pool"].append(strategy_id)
        _ensure_meta(state, strategy_id)
        _write(state)
        return state


def remove(strategy_id: str) -> dict:
    """移出策略池 (不归档不删除, 只退出扫描)。"""
    with _LOCK:
        state = _read()
        if strategy_id not in state["pool"]:
            raise KeyError(strategy_id)
        state["pool"] = [sid for sid in state["pool"] if sid != strategy_id]
        _write(state)
        return state


# ── 策略库元数据 (分组 / 归档, 与池无关) ──────────────────


def archive(strategy_id: str) -> dict:
    """归档策略: 库级标记 + 移出池 (可逆; 不删除策略文件)。"""
    with _LOCK:
        state = _read()
        record = _ensure_meta(state, strategy_id)
        record["archived"] = True
        record["archived_at"] = _now()
        if strategy_id in state["pool"]:
            state["pool"] = [sid for sid in state["pool"] if sid != strategy_id]
        _write(state)
        return state


def unarchive(strategy_id: str) -> dict:
    """恢复策略: 解除归档回到策略库; 不自动回池, 由用户重新勾选。"""
    with _LOCK:
        state = _read()
        record = state["meta"].get(strategy_id)
        if record is None or not record["archived"]:
            raise KeyError(strategy_id)
        record["archived"] = False
        record["archived_at"] = None
        _write(state)
        return state


def discard_record(strategy_id: str) -> dict:
    """删除策略的库级元数据记录 (档案库里清理已删除策略时用)。"""
    with _LOCK:
        state = _read()
        if strategy_id not in state["meta"]:
            raise KeyError(strategy_id)
        del state["meta"][strategy_id]
        state["pool"] = [sid for sid in state["pool"] if sid != strategy_id]
        _write(state)
        return state


def set_strategy_group(strategy_id: str, group_id: str | None) -> dict:
    """设置策略的自定义分组 (None = 未分组)。"""
    with _LOCK:
        state = _read()
        _validate_group_id(group_id, state["groups"])
        _ensure_meta(state, strategy_id)["group"] = group_id
        _write(state)
        return state


# ── 分组定义 (删除分组只摘标签, 不动策略) ──────────────────


def create_group(name: str) -> tuple[dict, dict]:
    with _LOCK:
        normalized = _normalize_group_name(name)
        state = _read()
        if any(g["name"].casefold() == normalized.casefold() for g in state["groups"]):
            raise ValueError("分组名称已存在")
        group = {"id": uuid.uuid4().hex, "name": normalized}
        state["groups"].append(group)
        _write(state)
        return state, group


def rename_group(group_id: str, name: str) -> dict:
    with _LOCK:
        normalized = _normalize_group_name(name)
        state = _read()
        target = next((g for g in state["groups"] if g["id"] == group_id), None)
        if target is None:
            raise KeyError(group_id)
        if any(g["id"] != group_id and g["name"].casefold() == normalized.casefold()
               for g in state["groups"]):
            raise ValueError("分组名称已存在")
        target["name"] = normalized
        _write(state)
        return state


def reorder_groups(ordered_ids: list[str]) -> dict:
    """按给定 id 顺序重排分组 (数组顺序即展示顺序)。"""
    with _LOCK:
        state = _read()
        by_id = {g["id"]: g for g in state["groups"]}
        if len(ordered_ids) != len(by_id) or set(ordered_ids) != set(by_id):
            raise ValueError("分组顺序与现有分组不一致")
        state["groups"] = [by_id[gid] for gid in ordered_ids]
        _write(state)
        return state


def delete_group(group_id: str) -> dict:
    """删除分组定义; 组内策略保留并转为未分组 (仅摘标签)。"""
    with _LOCK:
        state = _read()
        if not any(g["id"] == group_id for g in state["groups"]):
            raise KeyError(group_id)
        state["groups"] = [g for g in state["groups"] if g["id"] != group_id]
        for record in state["meta"].values():
            if record["group"] == group_id:
                record["group"] = None
        _write(state)
        return state
