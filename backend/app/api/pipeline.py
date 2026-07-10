"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging

from fastapi import APIRouter, HTTPException, Request

from app.jobs import daily_pipeline
from app.services.pipeline_jobs import job_store, release_run_slot, try_acquire_run_slot
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

    # 检测卡死的 running job (如 reload 后孤儿 task / 网络读无限阻塞)。
    # reap_stale 会在 /run 和 /jobs/{id} 轮询端点都调用,保证卡死后能自愈。
    job_store.reap_stale()

    # 单飞: 复用任何活跃 (pending∨running) 任务, is_new=False 时不再调度新任务
    job_id, is_new = job_store.create()
    if not is_new:
        return {"job_id": job_id, "reused": True}

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        # 重任务执行槽: 防僵尸并发(reap 后线程仍活时新任务不得并行写 parquet)
        if not try_acquire_run_slot():
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = getattr(request.app.state, "quote_service", None)
        try:
            job_store.start(job_id)
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                         skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            def _run() -> dict:
                if qs:
                    with qs.paused():
                        return daily_pipeline.run_now(repo, capset, on_progress=progress)
                return daily_pipeline.run_now(repo, capset, on_progress=progress)

            result = await loop.run_in_executor(_long_task_executor, _run)
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
            repo.refresh_cache()  # 刷新 Polars 缓存
        except Exception as e:  # noqa: BLE001
            logger.exception("pipeline failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot()

    asyncio.create_task(task())
    return {"job_id": job_id, "reused": False}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    # 每次轮询都检查卡死 job — 前端每秒轮询,STALE_JOB_TIMEOUT_S(10min)后必定自愈,
    # 无需用户再次手动点「同步」。
    job_store.reap_stale()
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
    return {"cancelled": job_id}


@router.get("/jobs")
def list_jobs(limit: int = 20) -> dict:
    return {
        "active_id": job_store.active_id(),
        "jobs": job_store.list_recent(limit=limit),
    }
