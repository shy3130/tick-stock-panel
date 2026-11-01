from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.api.auth import COOKIE_NAME, _client_ip
from app.data_providers.custom import packages
from app.services import auth


def require_management(request: Request):
    # Unlike read-only APIs, uploading Python is code execution. LAN access without
    # a password is not enough. No API-token scope grants this permission.
    if request.headers.get("authorization"):
        raise HTTPException(403, "插件安装仅供面板管理会话使用")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "插件管理需要同源访问")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "拒绝跨站插件管理请求")
    if auth.is_configured():
        if not auth.is_valid_session(request.cookies.get(COOKIE_NAME, "")):
            raise HTTPException(401, "请先登录")
    elif _client_ip(request) not in ("127.0.0.1", "::1", "localhost") or request.url.hostname not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "请先设置访问密码并登录，或通过本机访问安装插件")


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


@router.post("/{name}/manage")
def manage(name: str, body: Action):
    try:
        return packages.manage(name, body.action)
    except (ValueError, OSError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc
