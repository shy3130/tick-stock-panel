"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging

from fastapi import APIRouter, HTTPException, Request

from app.jobs import daily_pipeline
from app.services.pipeline_jobs import job_store
from app.api.data import invalidate_storage_cache

# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。

    若已有任务在跑,**返回该任务 id 而不是开新任务**(防止并发拉数据撞限流)。
    但如果该任务已运行超过 10 分钟 (可能因 reload 卡死), 强制标记为失败后重新创建。
    """
    repo = request.app.state.repo
    capset = request.app.state.capabilities

    # 检测卡死的 running job (如 reload 后孤儿 task)
    existing_id = job_store.active_id()
    if existing_id:
        existing = job_store.get(existing_id)
        if existing and existing["status"] == "running":
            from datetime import datetime, timezone
            started = existing.get("started_at")
            if started:
                try:
                    start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
                    if elapsed > 600:  # 超过 10 分钟视为卡死
                        logger.warning("强制取消卡死 job %s (已运行 %.0fs)", existing_id, elapsed)
                        job_store.fail(existing_id, "超时自动取消 (疑似 reload 后孤儿 task)")
                        invalidate_storage_cache()
                except Exception:
                    pass

    job_id = job_store.create(kind="daily_pipeline")

    # 如果是复用的 active job,直接返回(不重启)
    existing = job_store.get(job_id)
    if existing and existing["status"] == "running":
        return {"job_id": job_id, "reused": True}

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        job_store.start(job_id)
        loop = asyncio.get_event_loop()

        def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                     skip_log: bool = False) -> None:
            job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

        try:
            result = await loop.run_in_executor(
                _long_task_executor,
                lambda: daily_pipeline.run_now(repo, capset, on_progress=progress),
            )
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
            repo.refresh_cache()  # 刷新 Polars 缓存
        except Exception as e:  # noqa: BLE001
            logger.exception("pipeline failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()

    asyncio.create_task(task())
    return {"job_id": job_id, "reused": False}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """手动取消一个 running 的 job。"""
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    if j["status"] not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"job status is {j['status']}, cannot cancel")
    job_store.fail(job_id, "用户手动取消")
    invalidate_storage_cache()
    return {"cancelled": job_id}


@router.get("/jobs")
def list_jobs(limit: int = 20) -> dict:
    return {
        "active_id": job_store.active_id(),
        "jobs": job_store.list_recent(limit=limit),
    }
