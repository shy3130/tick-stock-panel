"""配置备份 API — 导出下载 / 导入预览 / 导入应用。

薄层: 参数接收 → service (config_backup.py) → 按 reload_hints 刷新进程内
引擎与数据源状态。路径白名单、内容校验、原子写、脱敏均在 service 层。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.data_providers.custom import loader as custom_loader
from app.services import config_backup
from app.strategy import monitor_rules

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config-backup", tags=["config-backup"])

# B008: FastAPI 要求参数默认值为 File(...) 调用, 提为模块级单例复用。
_UPLOAD_FILE = File(...)


def _category_param(raw: str) -> frozenset[str] | None:
    """categories 逗号串 → 类别集合。空 = 不传 = 全量; 非法键由 service 丢弃。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


@router.get("/export")
def export_backup(categories: str = "") -> Response:
    """导出用户配置为 ZIP 下载。categories 逗号分隔类别键, 不传导出全部 (不含密钥);
    显式包含 "secrets" 才打包明文密钥。"""
    data, filename = config_backup.build_backup(categories=_category_param(categories))
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > config_backup.MAX_ARCHIVE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"备份文件超过 {config_backup.MAX_ARCHIVE_BYTES // 1024 // 1024}MB 上限",
        )
    return data


@router.post("/inspect")
async def inspect_backup(file: UploadFile = _UPLOAD_FILE) -> dict:
    """解析备份给出预览 (类别/数量/冲突), 不写任何文件。"""
    data = await _read_upload(file)
    try:
        return config_backup.inspect_archive(data)
    except config_backup.ConfigBackupError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/apply")
async def apply_backup(
    request: Request,
    file: UploadFile = _UPLOAD_FILE,
    allow_overwrite: bool = Form(False),
    categories: str = Form(""),
) -> dict:
    data = await _read_upload(file)
    try:
        result = config_backup.apply_archive(
            data,
            allow_overwrite=allow_overwrite,
            categories=_category_param(categories),
        )
    except config_backup.ConfigBackupError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _reload_states(request, result.get("reload_hints") or {})
    return result


def _reload_states(request: Request, hints: dict) -> None:
    """按导入结果刷新进程内状态 (与 monitor_rules API 的 _sync_engine 同模式)。"""
    if hints.get("strategies"):
        engine = getattr(request.app.state, "strategy_engine", None)
        if engine is not None:
            engine.reload()
    if hints.get("monitor_rules"):
        monitor_engine = getattr(request.app.state, "monitor_engine", None)
        if monitor_engine is not None:
            rules = monitor_rules.load_all(request.app.state.repo.store.data_dir)
            monitor_engine.set_rules(rules)
    if hints.get("data_sources"):
        custom_loader.load_all()
