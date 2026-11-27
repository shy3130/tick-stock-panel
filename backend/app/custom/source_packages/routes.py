from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.api.auth import require_management as require_management
from app.data_providers.custom import packages


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        from app.data_providers import custom
        for plugin in custom.list_plugins():
            if plugin.get("origin") == "external":
                custom.get_provider(plugin["name"]).close()


router = APIRouter(lifespan=lifespan, prefix="/api/custom/source-packages", tags=["source-packages"], dependencies=[Depends(require_management)])


async def read_upload(file: UploadFile):
    try:
        data = await file.read(packages.MAX_UPLOAD + 1)
        if len(data) > packages.MAX_UPLOAD:
            raise HTTPException(413, "插件包超过 20 MiB")
        return data
    finally:
        await file.close()


@router.post("/inspect")
async def inspect(file: Annotated[UploadFile, File()]):
    data = await read_upload(file)
    from starlette.concurrency import run_in_threadpool
    try:
        preview, _ = await run_in_threadpool(packages.inspect_archive, data)
        return preview
    except (ValueError, OSError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/install")
async def install(file: Annotated[UploadFile, File()], digest: str = Form(...), revision: str | None = Form(None)):
    from starlette.concurrency import run_in_threadpool
    data = await read_upload(file)
    try:
        return await run_in_threadpool(packages.install, data, digest, revision)
    except (ValueError, OSError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc


class Action(BaseModel):
    action: str
    reset_routes: bool = False


@router.post("/{name}/manage")
def manage(name: str, body: Action, request: Request):
    try:
        result = packages.manage(name, body.action, reset_routes=body.reset_routes)
    except (ValueError, OSError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc
    from app.tickflow.policy import detect_capabilities
    request.app.state.capabilities = detect_capabilities()
    return result
