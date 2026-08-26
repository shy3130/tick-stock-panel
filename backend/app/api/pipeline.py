"""盘后管道 API — 异步触发 + 进度跟踪。"""

from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging

from fastapi import APIRouter, HTTPException, Request

from app.jobs import daily_pipeline
from app.services.pipeline_jobs import JobCancelledError, job_store
from app.api.data import invalidate_storage_cache

# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。

    单飞: 已有 pending/running 任务时返回该任务 id(reused=True),不重启。
    已收敛终态但 worker 尚未 release 执行槽时返回 409，不伪装成已调度。
    """
    repo = request.app.state.repo
    capset = request.app.state.capabilities

    job_store.reap_stale()
    execution_owner = job_store.execution_owner()
    if execution_owner is not None and job_store.active_id() != execution_owner:
        raise HTTPException(
            status_code=409,
            detail="上一数据任务已结束记录但执行线程仍在退出，请稍后重试",
        )

    job_id, is_new = job_store.create(kind="daily_pipeline")

    if not is_new:
        return {"job_id": job_id, "reused": True}

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        owner = job_store.start(job_id)
        if owner is None:
            return  # create 与执行之间被取消,放弃执行
        loop = asyncio.get_event_loop()

        def progress(
            stage: str, pct: int, msg: str, stage_pct: int | None = None, skip_log: bool = False
        ) -> None:
            job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

        try:
            result = await loop.run_in_executor(
                _long_task_executor,
                lambda: daily_pipeline.run_now(repo, capset, on_progress=progress),
            )
            job_store.succeed(job_id, result, owner=owner)
            invalidate_storage_cache()
            repo.refresh_cache()  # 刷新 Polars 缓存
        except JobCancelledError:
            logger.info("pipeline cancelled: job_id=%s", job_id)
            invalidate_storage_cache()
        except Exception as e:  # noqa: BLE001
            logger.exception("pipeline failed")
            job_store.fail(job_id, str(e), owner=owner)
            invalidate_storage_cache()
        finally:
            job_store.release(job_id, owner)

    asyncio.create_task(task())
    return {"job_id": job_id, "reused": False}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job_store.reap_stale()
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """手动取消一个 pending/running 的 job → cancelled 终态。

    执行中的 worker 在下一次 progress() 收到 JobCancelledError 协作退出;
    未认领的 pending job 直接落盘 cancelled。
    """
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    if j["status"] not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"job status is {j['status']}, cannot cancel")
    if not job_store.cancel(job_id):
        raise HTTPException(status_code=409, detail="job already finished")
    invalidate_storage_cache()
    return {"cancelled": job_id}


@router.get("/jobs")
def list_jobs(limit: int = 20) -> dict:
    return {
        "active_id": job_store.active_id(),
        "jobs": job_store.list_recent(limit=limit),
    }
