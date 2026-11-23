from __future__ import annotations

import hashlib
import io
import sqlite3
from typing import Annotated, Literal

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from tivelo_contracts.base import Digest
from tivelo_contracts.signatures import MAX_ENVELOPE

from app.api import ext_pages
from app.api.definitions import _uploads
from app.custom.source_packages.routes import read_upload, require_management
from app.extensions.packages import ExtensionPackageStore
from app.services import import_center
from app.services.engine_installation import EngineInstallation
from app.services.engine_jobs.storage import JobError
from app.services.engine_package_store import EnginePackageStore
from app.services.ext_pages import PagePackStore
from app.services.package_archive import read_code_archive

router = APIRouter(prefix="/api", tags=["import-center"])
_MANAGE = [Depends(require_management)]


def _store(request: Request) -> ExtensionPackageStore:
    return ExtensionPackageStore(request.app.state.repo.store.data_dir)


@router.post("/import-center/inspect")
async def inspect(request: Request, files: Annotated[list[UploadFile], File()]):
    try:
        uploads = await _uploads(files)
        return await run_in_threadpool(import_center.inspect_uploads, uploads, request.app.state.repo.store.data_dir)
    except (ValueError, OSError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except sqlite3.Error:
        raise HTTPException(503, "引擎候选索引暂不可用") from None
    finally:
        for file in files:
            await file.close()


@router.get("/extension-packages")
def list_packages(request: Request):
    return _store(request).list_installed(getattr(request.app.state, "managed_extension_versions", {}))


@router.get("/engine-extension-packages")
def engine_candidates(request: Request):
    try:
        return EnginePackageStore(request.app.state.repo.store.data_dir).list_staged()
    except (ValueError, OSError, sqlite3.Error):
        raise HTTPException(503, "引擎候选索引暂不可用") from None


class EngineSwitchBody(BaseModel):
    operation: Literal["activate", "rollback", "disable"]
    generation: int = Field(strict=True, ge=0)


@router.get("/engine-licenses", dependencies=_MANAGE)
def engine_licenses(request: Request):
    try:
        return EngineInstallation(request.app.state.repo.store.data_dir).status()
    except (ValueError, OSError, sqlite3.Error):
        raise HTTPException(503, "安装凭证存储暂不可用") from None


@router.post("/engine-licenses/identity", dependencies=_MANAGE)
def create_engine_identity(request: Request):
    try:
        return EngineInstallation(request.app.state.repo.store.data_dir).identity(create=True)
    except (ValueError, OSError, sqlite3.Error):
        raise HTTPException(503, "暂时无法创建或读取本机安装身份") from None


@router.post("/engine-extension-packages/{digest}/license", dependencies=_MANAGE)
async def import_engine_license(digest: str, request: Request, file: Annotated[UploadFile, File()],
                                revision: int = Form(ge=0)):
    store = EnginePackageStore(request.app.state.repo.store.data_dir)
    try:
        content = await file.read(MAX_ENVELOPE + 1)
        if not 0 < len(content) <= MAX_ENVELOPE:
            raise HTTPException(400, "专业使用凭证大小无效")
        return await run_in_threadpool(store.import_lease, digest, content, revision)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "安装凭证存储暂不可用") from None
    finally:
        await file.close()


@router.post("/engine-extension-packages/{digest}/switch", dependencies=_MANAGE)
async def switch_engine(digest: str, body: EngineSwitchBody, request: Request):
    store = EnginePackageStore(request.app.state.repo.store.data_dir)
    try:
        return await run_in_threadpool(store.switch, digest, body.operation, body.generation)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "引擎版本切换未完成，请刷新后重试") from None


class ProUninstallBody(BaseModel):
    generation: int = Field(strict=True, ge=0)
    digests: tuple[Digest, ...] = Field(min_length=1)


@router.post("/engine-extension-packages/pro-dynamic/uninstall", dependencies=_MANAGE)
def uninstall_pro_dynamic(body: ProUninstallBody, request: Request):
    manager = getattr(request.app.state, "engine_job_manager", None)
    if manager is None:
        raise HTTPException(503, "研究任务服务暂不可用，请重启后重试卸载")
    try:
        return manager.uninstall_price_extension(
            "tivelo.pro.dynamic-adjustment", body.generation, body.digests
        )
    except (ValueError, JobError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "卸载未完成，请检查扩展存储目录后重试") from None


@router.post("/engine-extension-packages/stage", dependencies=_MANAGE)
async def stage_engine(request: Request, file: Annotated[UploadFile, File()], digest: str = Form(...), revision: str = Form(...)):
    store = EnginePackageStore(request.app.state.repo.store.data_dir)
    try:
        return await run_in_threadpool(store.stage, await read_upload(file), digest, revision)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "引擎候选索引暂不可用") from None


@router.post("/extension-packages/install", dependencies=_MANAGE)
async def install(request: Request, file: Annotated[UploadFile, File()], digest: str = Form(...),
                  revision: str = Form(...), trusted: bool = Form(False)):
    if not trusted:
        await file.close()
        raise HTTPException(403, "请先确认扩展代码来源可信")
    try:
        return await run_in_threadpool(_store(request).install, await read_upload(file), digest, revision)
    except (ValueError, OSError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/extension-packages/frontend", dependencies=_MANAGE)
async def frontend_for_trial(request: Request, file: Annotated[UploadFile, File()],
                             digest: str = Form(...), trusted: bool = Form(False)):
    """Return a trusted candidate for browser-side contract checks; never publish it."""
    if not trusted:
        await file.close()
        raise HTTPException(403, "请先确认扩展代码来源可信")
    data = await read_upload(file)
    try:
        preview = await run_in_threadpool(_store(request).inspect, data)
        if preview["digest"] != digest:
            raise ValueError("扩展包与预览不一致")
        entry = preview["manifest"].get("frontend")
        if not entry:
            raise ValueError("扩展包没有前端页面")
        files = await run_in_threadpool(read_code_archive, data, frontend=True)
        return Response(files[entry], media_type="text/javascript", headers={"Cache-Control": "no-store"})
    except (ValueError, OSError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc


class ManageBody(BaseModel):
    action: str
    revision: str | None = None


@router.post("/extension-packages/{extension_id}/manage", dependencies=_MANAGE)
def manage(extension_id: str, body: ManageBody, request: Request):
    try:
        return _store(request).manage(extension_id, body.action, body.revision)
    except (ValueError, OSError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/import-center/page", dependencies=_MANAGE)
async def install_page(request: Request, file: Annotated[UploadFile, File()], id: str = Form(...),
                       digest: str = Form(...), trusted: bool = Form(False), overwrite: bool = Form(False)):
    if not trusted:
        await file.close()
        raise HTTPException(403, "请先确认页面代码来源可信")
    try:
        data = await file.read(5 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise HTTPException(409, "页面文件与预览不一致，请重新预览")
        store = PagePackStore(request.app.state.repo.store.data_dir)
        if store.get(id) and not overwrite:
            raise HTTPException(409, "页面标识已存在，请确认替换或更换标识")
        candidate = UploadFile(filename=file.filename, file=io.BytesIO(data))
        try:
            if (file.filename or "").lower().endswith(".tsx"):
                return await ext_pages.import_source_pack(request, candidate, id)
            return await ext_pages.upload_page_pack(request, id, candidate)
        finally:
            await candidate.close()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        await file.close()


class ProjectPageBody(BaseModel):
    namespace: str
    id: str
    trusted: bool = False
    overwrite: bool = False


@router.post("/import-center/project", dependencies=_MANAGE)
def import_project_page(request: Request, body: ProjectPageBody):
    if not body.trusted:
        raise HTTPException(403, "请先确认页面代码来源可信")
    try:
        if PagePackStore(request.app.state.repo.store.data_dir).get(body.id) and not body.overwrite:
            raise HTTPException(409, "页面标识已存在，请确认替换或更换标识")
        return ext_pages.import_source_from_project(
            request, ext_pages.ImportFromProjectBody(namespace=body.namespace, id=body.id),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
