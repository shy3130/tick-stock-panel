"""统一定义目录与导入冒烟 API (P4) — 一切皆扩展的消费面。

GET  /api/definitions         → 跨 kind 目录 (统一选择器的数据半边)
POST /api/definitions/smoke   → 导入冒烟 (校验 + 编译 + 依赖可解析)
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import definitions as defs
from app.services import resource_bundle

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


class ResourceRef(BaseModel):
    kind: Literal["strategy", "signal", "factor"]
    id: str = Field(min_length=1, max_length=128)


class ResourceExportRequest(BaseModel):
    resources: list[ResourceRef] = Field(min_length=1, max_length=2000)
    revision: str | None = None


@router.get("/resources")
def list_resources(request: Request) -> dict:
    return {"items": resource_bundle.list_resources(_data_dir(request), getattr(request.app.state, "strategy_engine", None))}


def _export(req: ResourceExportRequest, request: Request):
    try:
        return resource_bundle.build_resources(_data_dir(request), getattr(request.app.state, "strategy_engine", None), [r.model_dump() for r in req.resources])
    except resource_bundle.ResourceBundleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/resources/export/preview")
def preview_resource_export(req: ResourceExportRequest, request: Request) -> dict:
    return _export(req, request)[2]


@router.post("/resources/export")
def export_resources(req: ResourceExportRequest, request: Request) -> Response:
    data, filename, preview = _export(req, request)
    if req.revision is not None and req.revision != preview["revision"]:
        raise HTTPException(status_code=409, detail="资源已变化，请重新预览后再下载")
    issues = [issue for entry in preview["entries"] for issue in entry["issues"]]
    if issues:
        raise HTTPException(status_code=400, detail="资源依赖不完整，请先修复后再导出: " + "; ".join(issues[:6]))
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


_RESOURCE_FILES = File(...)


async def _uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    from app.services.config_backup import MAX_ARCHIVE_BYTES, MAX_ENTRIES

    if len(files) > MAX_ENTRIES:
        raise HTTPException(status_code=400, detail="上传文件数量超过上限")
    result = []
    remaining = MAX_ARCHIVE_BYTES
    for file in files:
        data = await file.read(remaining + 1)
        remaining -= len(data)
        if remaining < 0:
            raise HTTPException(status_code=400, detail="上传文件总量超过 128MB 上限")
        result.append((file.filename or "", data))
    return result


@router.post("/resources/inspect")
async def inspect_resources(files: list[UploadFile] = _RESOURCE_FILES) -> dict:
    try:
        return await run_in_threadpool(resource_bundle.inspect_resources, await _uploads(files))
    except resource_bundle.ResourceBundleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/resources/apply")
async def apply_resources(request: Request, files: list[UploadFile] = _RESOURCE_FILES,
                          selected: str = Form(...), overwrite: str = Form("[]"), revision: str = Form(...)) -> dict:
    try:
        selected_keys, overwrite_keys = json.loads(selected), json.loads(overwrite)
        if not all(isinstance(keys, list) and all(isinstance(k, str) for k in keys) for keys in (selected_keys, overwrite_keys)):
            raise ValueError("选择清单必须是资源 ID 数组")
        result = await run_in_threadpool(resource_bundle.apply_resources, await _uploads(files), selected=selected_keys, overwrite=overwrite_keys,
                                        revision=revision, engine=getattr(request.app.state, "strategy_engine", None), refresh=lambda: _refresh_resources(request))
    except (resource_bundle.ResourceBundleError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


def _refresh_resources(request: Request) -> None:
    from app.indicators.pipeline import invalidate_custom_signals
    from app.services import strategy_cache
    from app.strategy import custom_signals

    custom_signals.invalidate_intraday_cache()
    invalidate_custom_signals()
    strategy_cache.clear_cache(_data_dir(request))
    repo = getattr(request.app.state, "repo", None)
    if repo is not None and hasattr(repo, "clear_cache"):
        repo.clear_cache()
    monitor = getattr(request.app.state, "monitor_engine", None)
    if monitor is not None:
        monitor.invalidate_strategy_state()
