"""二次开发升级保护 API — 修改文件常备备份状态 / 扫描 / 升级报告 / 恢复。

见 app/services/upgrade_guard.py: 源码运行 (无清单) 时 enabled=false,
本组端点仍可调用, 返回停用原因。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.upgrade_guard import UpgradeGuard, UpgradeGuardBusyError

router = APIRouter(prefix="/api/upgrade-guard", tags=["upgrade-guard"])


def _guard(request: Request) -> UpgradeGuard:
    guard = getattr(request.app.state, "upgrade_guard", None)
    if guard is None:  # lifespan 未跑到 (测试直挂路由等) — 惰性兜底
        from app.config import settings

        guard = UpgradeGuard(Path(settings.data_dir))
        request.app.state.upgrade_guard = guard
    return guard


@router.get("/status")
def upgrade_guard_status(request: Request) -> dict:
    """守卫状态: 是否启用 / 当前修改与新增文件 / 待处理升级报告。"""
    guard = _guard(request)
    try:
        status = guard.status() if not guard.is_scanning() else {"enabled": True, "scanning": True}
    except UpgradeGuardBusyError:
        return {"enabled": True, "scanning": True}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="升级备份不可用，请检查磁盘空间、目录权限或备份状态") from exc
    status["scanning"] = guard.is_scanning()
    return status


@router.post("/scan")
def upgrade_guard_scan(request: Request) -> dict:
    """立即重扫 (增量缓存, 秒级); 修改程序文件后可手动刷新常备备份。"""
    guard = _guard(request)
    if guard.is_scanning():
        raise HTTPException(status_code=409, detail="扫描正在进行中")
    try:
        status = guard.scan()
    except UpgradeGuardBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="升级备份未完成，请检查磁盘空间、目录权限或备份状态后重试") from exc
    status["scanning"] = False
    return status


class RestoreIn(BaseModel):
    report_id: str = Field(min_length=1, max_length=200)
    relpath: str = Field(min_length=1, max_length=500)


@router.post("/restore")
def upgrade_guard_restore(req: RestoreIn, request: Request) -> dict:
    """把升级报告归档的用户版本复制回程序目录 (仅限报告清单内文件)。"""
    guard = _guard(request)
    try:
        restored = guard.restore(req.report_id, req.relpath)
    except UpgradeGuardBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as exc:
        raise HTTPException(status_code=503, detail="文件恢复失败，请检查程序目录权限后重试") from exc
    return {"ok": True, "restored": str(restored)}


@router.post("/dismiss-report")
def upgrade_guard_dismiss(request: Request) -> dict:
    """确认升级报告, 最近一份归档保留到下次升级报告成功替换。"""
    try:
        _guard(request).dismiss_report()
    except UpgradeGuardBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="升级报告确认失败，请检查备份目录后重试") from exc
    return {"ok": True}
