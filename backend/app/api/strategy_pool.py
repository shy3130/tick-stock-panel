"""策略池 API — 池成员 / 策略库分组与归档 / 档案库的读写胶水。

薄层: 参数校验 → services.strategy_pool → 快照。GET 时把引擎当前策略
ID 同步进 meta (惰性登记加入时间), 让「按加入时间排序」立即生效。
池是界面状态, 写操作不做策略引擎存在性校验 (前端按策略列表 prune 失效
ID, 与 v1 行为一致)。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import strategy_pool

router = APIRouter(prefix="/api/strategy-pool", tags=["strategy-pool"])


def _snapshot(payload: dict) -> dict:
    return {key: payload[key] for key in ("revision", "groups", "pool", "meta")}


def _engine_strategy_ids(request: Request) -> list[str]:
    """当前引擎已加载的策略 ID; 引擎不可用时退化为空 (meta 不补登记)。"""
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        return []
    try:
        return [strategy.id for strategy in engine.list_strategies()]
    except Exception:  # noqa: BLE001 — 引擎瞬时不可用不应拖垮池快照
        return []


@router.get("")
def get_pool(request: Request) -> dict:
    state = strategy_pool.sync_known_ids(_engine_strategy_ids(request))
    return _snapshot(state)


class PoolIn(BaseModel):
    """池的整体替换载荷 (纯 ID 顺序)。"""

    pool: list[str] = Field(max_length=500)
    only_if_empty: bool = False
    expected_revision: int | None = Field(default=None, ge=0)


@router.put("/entries")
def put_entries(req: PoolIn) -> dict:
    try:
        return _snapshot(strategy_pool.set_pool(
            req.pool, only_if_empty=req.only_if_empty, expected_revision=req.expected_revision,
        ))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/entries/{strategy_id}")
def add_entry(strategy_id: str) -> dict:
    try:
        return _snapshot(strategy_pool.add(strategy_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/entries/{strategy_id}")
def remove_entry(strategy_id: str) -> dict:
    try:
        return _snapshot(strategy_pool.remove(strategy_id))
    except KeyError as exc:
        raise HTTPException(404, "该策略不在策略池中") from exc


# ── 策略库元数据: 归档 / 分组 ────────────────────────────


@router.post("/strategies/{strategy_id}/archive")
def archive_strategy(strategy_id: str) -> dict:
    """归档策略 (库级, 可逆): 移入档案库并移出池; 不删除策略文件。"""
    return _snapshot(strategy_pool.archive(strategy_id))


@router.post("/strategies/{strategy_id}/unarchive")
def unarchive_strategy(strategy_id: str) -> dict:
    """恢复策略: 回到策略库 (不自动回池)。"""
    try:
        return _snapshot(strategy_pool.unarchive(strategy_id))
    except KeyError as exc:
        raise HTTPException(404, "该策略不在档案库中") from exc


@router.delete("/strategies/{strategy_id}/record")
def discard_record(strategy_id: str) -> dict:
    """删除该策略的库级记录 (清理档案库中已删除的策略; 策略文件不受影响)。"""
    try:
        return _snapshot(strategy_pool.discard_record(strategy_id))
    except KeyError as exc:
        raise HTTPException(404, "该策略没有库级记录") from exc


class GroupIn(BaseModel):
    group: str | None = None


@router.post("/strategies/{strategy_id}/group")
def set_strategy_group(strategy_id: str, req: GroupIn) -> dict:
    try:
        return _snapshot(strategy_pool.set_strategy_group(strategy_id, req.group))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 分组定义 ────────────────────────────────────────────


class GroupNameIn(BaseModel):
    name: str = Field(min_length=1, max_length=24)


class GroupReorderIn(BaseModel):
    ordered_ids: list[str] = Field(min_length=0, max_length=50)


@router.post("/groups")
def create_group(req: GroupNameIn) -> dict:
    try:
        state, group = strategy_pool.create_group(req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**_snapshot(state), "group": group}


@router.put("/groups/reorder")
def reorder_groups(req: GroupReorderIn) -> dict:
    try:
        return _snapshot(strategy_pool.reorder_groups(req.ordered_ids))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/groups/{group_id}")
def rename_group(group_id: str, req: GroupNameIn) -> dict:
    try:
        return _snapshot(strategy_pool.rename_group(group_id, req.name))
    except KeyError as exc:
        raise HTTPException(404, "分组不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/groups/{group_id}")
def delete_group(group_id: str) -> dict:
    """删除分组定义; 组内策略保留并转为未分组。"""
    try:
        return _snapshot(strategy_pool.delete_group(group_id))
    except KeyError as exc:
        raise HTTPException(404, "分组不存在") from exc
