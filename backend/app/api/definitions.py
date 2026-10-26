"""统一定义目录与导入冒烟 API (P4) — 一切皆扩展的消费面。

GET  /api/definitions         → 跨 kind 目录 (统一选择器的数据半边)
POST /api/definitions/smoke   → 导入冒烟 (校验 + 编译 + 依赖可解析)
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app import definitions as defs

router = APIRouter(prefix="/api/definitions", tags=["definitions"])


class SmokeRequest(BaseModel):
    kind: str = Field(pattern="^(signal|factor)$")
    definition: dict | None = None
    # 二选一: 传 id 时按存储重查该定义复检 (统一选择器的"逐条冒烟"),
    # 不必先把原始定义回传服务端。
    id: str | None = None


@router.get("")
def list_definitions(request: Request) -> dict:
    return {"items": defs.catalog(_data_dir(request))}


@router.post("/smoke")
def smoke_definition(req: SmokeRequest) -> dict:
    """导入冒烟: 返回 ok/issues/dependencies/warmup_bars, 不落盘。"""
    if req.id is not None:
        return defs.smoke_stored(req.kind, req.id, _data_dir(None))
    if req.definition is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="definition 与 id 必须提供其一")
    return defs.smoke_definition(req.kind, req.definition)


def _data_dir(request: Request):
    from app.config import settings

    _ = request  # 预留 request 级覆写口; 当前与各 API 一致用全局 settings
    return settings.data_dir
