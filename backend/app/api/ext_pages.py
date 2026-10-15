"""运行时页面插件 API — 上传/清单回填/启停/删除/bundle 分发。

分发端点位于 /api 前缀下, 自动受集中鉴权中间件保护: 浏览器同源动态
import 会携带登录 cookie, 与页面会话同一访问边界。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services.ext_pages import PagePackMeta, PagePackStore

router = APIRouter(prefix="/api/ext-pages", tags=["ext-pages"])

# 单文件 ES 模块产物通常 < 1MB, 5MB 上限留足余量; 分块写防 OOM(同 ext_data)
_MAX_BUNDLE_BYTES = 5 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _write_bundle_capped(file: UploadFile, dest: Path, max_bytes: int) -> None:
    """分块写入, 累计超限立即 413 并停止读取(防内存放大)。"""
    total = 0
    with dest.open("wb") as f:
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(413, f"页面插件过大(上限 {max_bytes // (1024 * 1024)}MB)")
            f.write(chunk)


def _store(request: Request) -> PagePackStore:
    return PagePackStore(request.app.state.repo.store.data_dir)


class ManifestBody(BaseModel):
    label: str = ""
    path: str = ""
    error: str = ""


class EnabledBody(BaseModel):
    enabled: bool


@router.post("/upload")
async def upload_page_pack(
    request: Request,
    id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> PagePackMeta:
    """上传页面插件产物(.js/.mjs), 覆盖式: 同 ID 重新上传即更新。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".js", ".mjs"):
        raise HTTPException(400, "仅支持 .js / .mjs 页面插件产物")
    store = _store(request)
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        tmp_path = tmp_dir / "bundle.js"
        await _write_bundle_capped(file, tmp_path, _MAX_BUNDLE_BYTES)
        if tmp_path.stat().st_size == 0:
            raise HTTPException(400, "页面插件为空文件")
        try:
            return store.save_upload(id, tmp_path)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.get("")
def list_page_packs(request: Request) -> list[PagePackMeta]:
    return _store(request).list()


@router.get("/{pack_id}/bundle.js")
def get_page_pack_bundle(pack_id: str, request: Request) -> FileResponse:
    """分发产物。no-store + ETag(sha256): 热更新产物后浏览器立即取新, 未变时可用缓存协商。"""
    store = _store(request)
    try:
        meta = store.get(pack_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    bundle = store.bundle_path(pack_id) if meta else None
    if meta is None or bundle is None:
        raise HTTPException(404, f"页面插件 '{pack_id}' 不存在")
    return FileResponse(
        bundle,
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{meta.sha256}"',
        },
    )


@router.put("/{pack_id}/manifest")
def put_page_pack_manifest(pack_id: str, body: ManifestBody, request: Request) -> PagePackMeta:
    """前端试载结果回填: error 非空 → 停用并记错误, 否则启用并登记摘要。"""
    store = _store(request)
    try:
        return store.update_manifest(pack_id, label=body.label, path=body.path, error=body.error)
    except KeyError:
        raise HTTPException(404, f"页面插件 '{pack_id}' 不存在") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.put("/{pack_id}/enabled")
def put_page_pack_enabled(pack_id: str, body: EnabledBody, request: Request) -> PagePackMeta:
    store = _store(request)
    try:
        return store.set_enabled(pack_id, body.enabled)
    except KeyError:
        raise HTTPException(404, f"页面插件 '{pack_id}' 不存在") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{pack_id}")
def delete_page_pack(pack_id: str, request: Request) -> dict:
    store = _store(request)
    try:
        if not store.delete(pack_id):
            raise HTTPException(404, f"页面插件 '{pack_id}' 不存在")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}
