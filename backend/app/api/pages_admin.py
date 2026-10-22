"""页面管理 API — 源码扩展页的物理删除(带强制备份)与备份下载。

删除语义 (维护者决策): 「删除」= 物理移除 frontend/src/custom/<ns>/ 源码目录,
dev 热重载/下次构建后该页面从产物消失。删除前必须先打包备份到
data/backups/pages/ (data/ 不进 git), 压缩失败则整个请求失败、目录保持原样;
误删可用 git checkout 或备份 zip 恢复。产物型部署 (Docker/桌面) 容器内没有
前端源码树, 一律 409 拒绝而不是静默无效。
"""
from __future__ import annotations

import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.config import settings

router = APIRouter(prefix="/api/pages-admin", tags=["pages-admin"])

# 与前端扩展注册表 ID 规则 (extensions/registry.ts ID_RE) 保持同形
_NS_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
# 模板目录是新建扩展的起点, 永不删除
_PROTECTED_NS = {"_template"}
# 只放行本模块生成的备份文件名, 兼作下载接口的穿越防御
_BACKUP_NAME_RE = re.compile(r"^[a-z0-9._-]+-\d{8}-\d{6}\.zip$")


def _custom_root() -> Path:
    """前端源码扩展根目录。

    源码部署时 settings.static_dir = <项目根>/frontend/dist, 由此反推 src/custom;
    产物部署 (Docker/桌面) 该路径不存在, 由调用方的 is_dir 检查转 409。
    """
    return settings.static_dir.parent / "src" / "custom"


def _backup_root(request: Request) -> Path:
    root = request.app.state.repo.store.data_dir / "backups" / "pages"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_namespace_dir(namespace: str) -> Path:
    root = _custom_root()
    if not root.is_dir():
        raise HTTPException(409, "当前部署没有前端源码目录, 仅源码部署支持删除扩展页")
    if not _NS_RE.match(namespace) or namespace in _PROTECTED_NS:
        raise HTTPException(400, f"非法的扩展命名空间: {namespace}")
    ns_dir = (root / namespace).resolve()
    if ns_dir.parent != root.resolve():
        raise HTTPException(400, "非法路径")
    if not ns_dir.is_dir():
        raise HTTPException(404, f"扩展命名空间不存在: {namespace}")
    return ns_dir


def _zip_dir(src: Path, dest: Path) -> None:
    """整目录打包, 归档内保留 <namespace>/ 前缀, 解压即得原目录结构。"""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            zf.write(p, p.relative_to(src.parent))


@router.delete("/source-extensions/{namespace}")
def delete_source_extension(namespace: str, request: Request) -> dict:
    """删除一个源码扩展页目录。先备份后删除, 备份失败不动源码。"""
    ns_dir = _resolve_namespace_dir(namespace)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = _backup_root(request) / f"{namespace}-{stamp}.zip"
    _zip_dir(ns_dir, backup)
    shutil.rmtree(ns_dir)
    return {"deleted": namespace, "backup": backup.name}


@router.get("/backups/{filename}")
def download_backup(filename: str, request: Request) -> FileResponse:
    """下载删除备份 (attachment, 浏览器存到下载文件夹)。"""
    if not _BACKUP_NAME_RE.match(filename):
        raise HTTPException(404, "备份不存在")
    root = _backup_root(request).resolve()
    p = (root / filename).resolve()
    if p.parent != root or not p.is_file():
        raise HTTPException(404, "备份不存在")
    return FileResponse(p, filename=filename, media_type="application/zip")
