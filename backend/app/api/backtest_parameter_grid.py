"""参数网格 API — 受限笛卡尔参数寻优实验。

端点:
  POST   /api/backtest/parameter-grid               启动实验
  GET    /api/backtest/parameter-grid/{id}           实验详情 + 进度
  GET    /api/backtest/parameter-grid/{id}/stream    SSE 实时进度
  POST   /api/backtest/parameter-grid/{id}/cancel    取消实验

不注册到 main.py (由主会话统一注册)。无 AI / 无订单语义。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.backtest import (
    FACTOR_DEFAULT_DAYS,
    _get_engine,
    _guard_server_backtest_range,
    _resolve_start,
)
from app.backtest.parameter_grid import (
    DEFAULT_MAX_SCENARIOS,
    GRID_MAX_WORKERS,
    HARD_MAX_SCENARIOS,
    ParameterGridExperimentStore,
    compute_config_hash,
    expand_scenarios,
    normalize_grid,
    run_grid,
)
from app.backtest.strategy import StrategyBacktestConfig

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


# ================================================================
# 请求模型
# ================================================================

class ParameterGridRequest(BaseModel):
    """参数网格请求 (复用策略回测基础参数 + 网格轴)。"""
    strategy_id: str
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    params: dict | None = None
    overrides: dict | None = None
    matching: Literal["close_t", "open_t+1"] = "open_t+1"
    entry_fill: Literal["close_t", "open_t+1"] | None = None
    exit_fill: Literal["close_t", "open_t+1"] | None = None
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    max_positions: int = 10
    max_exposure_pct: float = 1.0
    initial_capital: float = 1_000_000.0
    position_sizing: Literal["equal", "score_weight"] = "equal"
    mode: Literal["position", "full"] = "position"
    holding_days: int = 5
    regime_filter: dict | None = None
    risk_free_rate: float = Field(default=0.0, gt=-1.0, le=1.0)
    # ── 网格轴 ──
    grid: dict[str, list[float]] = Field(..., description="参数网格: {param_id: [候选值]}")
    objective: Literal["sharpe", "calmar", "total_return", "risk_adjusted"] = "risk_adjusted"
    max_scenarios: int = Field(DEFAULT_MAX_SCENARIOS, ge=1, le=HARD_MAX_SCENARIOS)


# ================================================================
# 模块级实验任务表 (SSE / cancel / 重连)
# ================================================================

class _GridJob:
    """单个网格实验的运行状态, 存模块级供 SSE 重连和 cancel。"""
    __slots__ = ("experiment_id", "config_hash", "cancel_event", "progress", "snapshot", "error", "done", "finish_ts")

    def __init__(self, experiment_id: str, config_hash: str) -> None:
        self.experiment_id = experiment_id
        self.config_hash = config_hash
        self.cancel_event = threading.Event()
        self.progress: list[dict] = []
        self.snapshot: dict | None = None  # 最新 experiment.to_dict()
        self.error: str | None = None
        self.done = False
        self.finish_ts: float = 0.0


_grid_jobs: dict[str, _GridJob] = {}
_grid_jobs_lock = threading.Lock()
_GRID_JOB_TTL = 600  # 完成后保留 10 分钟 (实验比单回测慢)


def _cleanup_stale_grid_jobs():
    now = time.time()
    stale = [k for k, j in _grid_jobs.items() if j.done and now - j.finish_ts > _GRID_JOB_TTL]
    for k in stale:
        _grid_jobs.pop(k, None)


def _find_running_job_by_hash(config_hash: str) -> _GridJob | None:
    """在运行中 (非 done) 的实验里查找 config_hash 匹配。"""
    for job in _grid_jobs.values():
        if job.config_hash == config_hash and not job.done:
            return job
    return None


# ================================================================
# 辅助: 从请求构建 StrategyBacktestConfig
# ================================================================

def _build_base_config(req: ParameterGridRequest, start: date, end: date) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=req.strategy_id,
        symbols=req.symbols if req.symbols else None,
        start=start,
        end=end,
        params=req.params,
        overrides=req.overrides,
        matching=req.matching,
        entry_fill=req.entry_fill,
        exit_fill=req.exit_fill,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
        max_positions=req.max_positions,
        max_exposure_pct=req.max_exposure_pct,
        initial_capital=req.initial_capital,
        position_sizing=req.position_sizing,
        mode=req.mode,
        holding_days=req.holding_days,
        regime_filter=req.regime_filter,
        risk_free_rate=req.risk_free_rate,
    )


def _get_store(request: Request) -> ParameterGridExperimentStore:
    return ParameterGridExperimentStore(request.app.state.repo.store.data_dir)


# ================================================================
# POST /parameter-grid — 启动实验
# ================================================================

@router.post("/parameter-grid")
async def parameter_grid_launch(req: ParameterGridRequest, request: Request):
    """启动参数网格实验, 返回 experiment_id + scenario_count + truncated。"""
    from app.backtest.strategy import StrategyBacktestService

    engine = _get_engine(request)
    strategy_engine = request.app.state.strategy_engine
    svc = StrategyBacktestService(engine, strategy_engine)

    # 解析日期 + 范围保护
    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    # 获取策略定义, 校验网格
    try:
        s = strategy_engine.get(req.strategy_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        ng = normalize_grid(req.grid, s.meta.get("params", []), req.objective, req.max_scenarios)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    base_config = _build_base_config(req, start, end)
    config_hash = compute_config_hash(base_config, ng.grid, ng.objective)
    experiment_id = f"pg-{uuid.uuid4().hex[:12]}"

    # config_hash 去重: 同配置已在运行 → 返回已有实验
    _cleanup_stale_grid_jobs()
    with _grid_jobs_lock:
        existing = _find_running_job_by_hash(config_hash)
        if existing is not None:
            return {
                "experiment_id": existing.experiment_id,
                "config_hash": config_hash,
                "scenario_count": ng.scenario_count,
                "truncated": ng.truncated,
                "status": "already_running",
            }

    scenarios = expand_scenarios(base_config, ng)
    store = _get_store(request)

    # 创建 job + 启动后台线程
    with _grid_jobs_lock:
        job = _GridJob(experiment_id, config_hash)
        _grid_jobs[experiment_id] = job

    def _progress_cb(evt: dict) -> None:
        job.progress.append(evt)
        # 从 store 重载最新快照供 GET / SSE
        try:
            exp = store.load(experiment_id)
            job.snapshot = exp.to_dict()
        except Exception:  # noqa: BLE001
            pass

    def _run_experiment():
        try:
            exp = run_grid(
                service=svc,
                store=store,
                base_config=base_config,
                scenarios=scenarios,
                ng=ng,
                experiment_id=experiment_id,
                config_hash=config_hash,
                max_workers=GRID_MAX_WORKERS,
                progress_cb=_progress_cb,
                cancel_event=job.cancel_event,
            )
            job.snapshot = exp.to_dict()
        except Exception as e:  # noqa: BLE001
            job.error = str(e)
        finally:
            job.done = True
            job.finish_ts = time.time()

    threading.Thread(target=_run_experiment, daemon=True).start()

    return {
        "experiment_id": experiment_id,
        "config_hash": config_hash,
        "scenario_count": ng.scenario_count,
        "requested_count": ng.requested_count,
        "truncated": ng.truncated,
        "objective": ng.objective,
        "status": "started",
    }


# ================================================================
# GET /parameter-grid/{id} — 实验详情 + 进度
# ================================================================

@router.get("/parameter-grid/{experiment_id}")
def parameter_grid_detail(experiment_id: str, request: Request):
    """获取实验详情: 运行中从内存 job 快照, 已完成从持久化 store。"""
    job = _grid_jobs.get(experiment_id)
    if job is not None and job.snapshot is not None:
        return job.snapshot
    # 内存无 (过期/重启) → 从 store 读取
    store = _get_store(request)
    try:
        return store.load(experiment_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ================================================================
# GET /parameter-grid/{id}/stream — SSE
# ================================================================

@router.get("/parameter-grid/{experiment_id}/stream")
async def parameter_grid_stream(experiment_id: str, request: Request):
    """SSE 流式推送实验进度。

    事件:
      - progress: {completed, total, scenario_id}
      - done: {experiment} (完整实验结果)
      - error: {message}
    """
    job = _grid_jobs.get(experiment_id)

    async def event_generator():
        # 内存无 job → 尝试 store (已完成的实验)
        if job is None:
            store = _get_store(request)
            try:
                exp = store.load(experiment_id)
                yield f"event: done\ndata: {json.dumps(exp.to_dict(), ensure_ascii=False, default=str)}\n\n"
            except (KeyError, ValueError):
                yield f"event: error\ndata: {json.dumps({'message': f'实验不存在: {experiment_id}'}, ensure_ascii=False)}\n\n"
            return

        cursor = 0
        tick = 0
        try:
            while True:
                if job.done:
                    if job.error:
                        yield f"event: error\ndata: {json.dumps({'message': job.error}, ensure_ascii=False)}\n\n"
                    elif job.snapshot is not None:
                        yield f"event: done\ndata: {json.dumps(job.snapshot, ensure_ascii=False, default=str)}\n\n"
                    return

                # 断开检测
                tick += 1
                if tick % 4 == 0 and await request.is_disconnected():
                    break

                # 推送新进度
                prog_list = job.progress
                while cursor < len(prog_list):
                    msg = prog_list[cursor]
                    cursor += 1
                    yield f"event: progress\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ================================================================
# POST /parameter-grid/{id}/cancel — 取消
# ================================================================

@router.post("/parameter-grid/{experiment_id}/cancel")
async def parameter_grid_cancel(experiment_id: str):
    """取消正在运行的实验。"""
    job = _grid_jobs.get(experiment_id)
    if job is not None and not job.done:
        job.cancel_event.set()
        return {"ok": True, "experiment_id": experiment_id}
    return {"ok": False, "message": "实验不存在或已完成"}
