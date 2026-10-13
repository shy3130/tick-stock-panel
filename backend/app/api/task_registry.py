"""进行中任务注册表 API — 前端 fire-and-forget 上报 + 页面挂载时查询。

PUT/DELETE 由任务启动/终态触发 (不阻塞 UI, 失败静默); GET 在任务页挂载时
最多调用一次。全部是小 JSON 文件读写, 不触碰任务执行路径。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import task_registry

router = APIRouter(prefix="/api/task-registry", tags=["task-registry"])

# id 由前端按 kind+参数指纹生成 (字母/数字/冒号/下划线/短横)
_VALID_ID = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")


class TaskUpsertRequest(BaseModel):
    kind: str = Field(max_length=32)
    status: str = Field(max_length=16)
    label: str = Field(default="", max_length=120)
    payload: dict = Field(default_factory=dict)


def _data_dir(request: Request):
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="数据目录不可用")
    return repo.store.data_dir


@router.get("")
def list_tasks(request: Request, kind: str | None = None) -> dict:
    if kind is not None and kind not in task_registry.TASK_KINDS:
        raise HTTPException(status_code=400, detail=f"未知的任务类型: {kind}")
    return {"items": task_registry.list_items(_data_dir(request), kind)}


@router.put("/{task_id}")
def upsert_task(request: Request, task_id: str, req: TaskUpsertRequest) -> dict:
    if not _VALID_ID.match(task_id):
        raise HTTPException(status_code=400, detail="非法的任务 id")
    try:
        entry = task_registry.upsert(
            _data_dir(request),
            task_id,
            kind=req.kind,
            status=req.status,
            label=req.label,
            payload=req.payload,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "item": entry}


@router.delete("/{task_id}")
def delete_task(request: Request, task_id: str) -> dict:
    removed = task_registry.delete(_data_dir(request), task_id)
    return {"ok": True, "removed": removed}
