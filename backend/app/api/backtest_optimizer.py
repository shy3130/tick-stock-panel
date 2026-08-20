"""策略寻优 API — 多轴训练/留出搜索。

端点:
  GET    /api/backtest/optimizer/universes
  POST   /api/backtest/optimizer
  GET    /api/backtest/optimizer/{id}
  GET    /api/backtest/optimizer/{id}/stream
  POST   /api/backtest/optimizer/{id}/cancel
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.backtest import _get_engine, _guard_server_backtest_range
from app.backtest.optimizer import (
    BOARD_LABELS,
    DEFAULT_HOLDING_DAYS,
    DEFAULT_INDUSTRY_TOP_N,
    DEFAULT_MAX_SCENARIOS,
    DEFAULT_MIN_TRADES,
    DEFAULT_TOP_K,
    DEFAULT_TRAIN_RATIO,
    DEFAULT_YEARS,
    HARD_MAX_SCENARIOS,
    OptimizerExperimentStore,
    build_universes,
    classify_board,
    compute_config_hash,
    expand_combo_specs,
    expand_search_scenarios,
    install_combo_strategies,
    leaf_strategy_ids,
    resolve_window,
    run_search,
    split_train_holdout,
    _validate_and_expand_param_grid,
)
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.services.market_overview_builder import symbol_dimension_map

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class OptimizerRequest(BaseModel):
    strategy_ids: list[str] = Field(..., min_length=1)
    symbols: list[str] | None = None
    include_all_a: bool = True
    boards: list[str] = Field(default_factory=lambda: ["main", "gem", "star", "bj"])
    industries: list[str] = Field(default_factory=list)
    industry_top_n: int = Field(default=0, ge=0, le=20)
    per_symbol: bool = False
    holding_days: list[int] = Field(default_factory=lambda: list(DEFAULT_HOLDING_DAYS))
    matchings: list[Literal["close_t", "open_t+1"]] = Field(default_factory=lambda: ["open_t+1"])
    years: int = Field(default=DEFAULT_YEARS, ge=1, le=15)
    end: date | None = None
    train_ratio: float = Field(default=DEFAULT_TRAIN_RATIO, ge=0.5, le=0.9)
    objective: Literal["sharpe", "calmar", "total_return", "risk_adjusted"] = "risk_adjusted"
    min_trades: int = Field(default=DEFAULT_MIN_TRADES, ge=1, le=500)
    max_drawdown: float | None = Field(default=None, gt=0.0, le=1.0)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=20)
    max_scenarios: int = Field(default=DEFAULT_MAX_SCENARIOS, ge=1, le=HARD_MAX_SCENARIOS)
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    max_positions: int = 10
    initial_capital: float = 1_000_000.0
    risk_free_rate: float = 0.0
    include_combos: bool = True
    param_grid: dict[str, dict[str, list]] | None = None



class _OptJob:
    __slots__ = ("experiment_id", "config_hash", "cancel_event", "progress", "snapshot", "error", "done", "finish_ts")

    def __init__(self, experiment_id: str, config_hash: str) -> None:
        self.experiment_id = experiment_id
        self.config_hash = config_hash
        self.cancel_event = threading.Event()
        self.progress: list[dict] = []
        self.snapshot: dict | None = None
        self.error: str | None = None
        self.done = False
        self.finish_ts: float = 0.0


_jobs: dict[str, _OptJob] = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 600


def _cleanup_stale_jobs() -> None:
    now = time.time()
    stale = [k for k, j in _jobs.items() if j.done and now - j.finish_ts > _JOB_TTL]
    for key in stale:
        _jobs.pop(key, None)


def _find_running(config_hash: str) -> _OptJob | None:
    for job in _jobs.values():
        if job.config_hash == config_hash and not job.done:
            return job
    return None


def _get_store(request: Request) -> OptimizerExperimentStore:
    return OptimizerExperimentStore(request.app.state.repo.store.data_dir)


def _latest_symbols(repo) -> list[str]:
    frame, _ = repo.get_enriched_latest()
    if frame is None or frame.is_empty() or "symbol" not in frame.columns:
        return []
    return [str(s) for s in frame["symbol"].to_list() if s]


def _industry_counts(industry_map: dict[str, list[str]]) -> list[dict]:
    inverted: dict[str, set[str]] = {}
    for sym, names in industry_map.items():
        if "." not in str(sym):
            continue
        for name in names:
            if name:
                inverted.setdefault(str(name), set()).add(str(sym))
    ranked = sorted(inverted.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [{"id": name, "label": name, "count": len(members)} for name, members in ranked[:40]]


@router.get("/optimizer/universes")
def optimizer_universes(request: Request):
    repo = request.app.state.repo
    latest = getattr(repo, "local_enriched_latest_date", lambda: None)()
    earliest = getattr(repo, "earliest_daily_date", lambda: None)()
    try:
        start, end, warnings = resolve_window(end=None, years=DEFAULT_YEARS, earliest=earliest, latest=latest)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    symbols = _latest_symbols(repo)
    board_counts = {key: 0 for key in BOARD_LABELS}
    for symbol in symbols:
        board = classify_board(symbol)
        if board in board_counts:
            board_counts[board] += 1
    try:
        industry_map = symbol_dimension_map(repo, "industry", level=2)
    except Exception:  # noqa: BLE001
        industry_map = {}
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "earliest": None if earliest is None else earliest.isoformat(),
        "latest": None if latest is None else latest.isoformat(),
        "years": DEFAULT_YEARS,
        "boards": [
            {"id": key, "label": BOARD_LABELS[key], "count": board_counts[key]}
            for key in BOARD_LABELS
        ],
        "industries": _industry_counts(industry_map if isinstance(industry_map, dict) else {}),
        "warnings": [*warnings, "survivorship_bias"],
        "limits": {
            "max_scenarios": HARD_MAX_SCENARIOS,
            "default_max_scenarios": DEFAULT_MAX_SCENARIOS,
            "per_symbol_max": 8,
        },
    }


@router.post("/optimizer")
async def optimizer_launch(req: OptimizerRequest, request: Request):
    engine = _get_engine(request)
    strategy_engine = request.app.state.strategy_engine
    for sid in req.strategy_ids:
        try:
            strategy_engine.get(sid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    repo = request.app.state.repo
    latest = getattr(repo, "local_enriched_latest_date", lambda: None)()
    earliest = getattr(repo, "earliest_daily_date", lambda: None)()
    try:
        start, end, window_warnings = resolve_window(
            end=req.end, years=req.years, earliest=earliest, latest=latest,
        )
        train_start, train_end, holdout_start, holdout_end = split_train_holdout(
            start, end, train_ratio=req.train_ratio,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _guard_server_backtest_range(start, end)

    try:
        industry_map = symbol_dimension_map(repo, "industry", level=2) if (req.industries or req.industry_top_n) else {}
    except Exception:  # noqa: BLE001
        industry_map = {}
    try:
        universes = build_universes(
            symbols=req.symbols,
            include_all_a=req.include_all_a,
            boards=req.boards,
            industries=req.industries,
            industry_map=industry_map if isinstance(industry_map, dict) else {},
            per_symbol=req.per_symbol,
            industry_top_n=req.industry_top_n if not req.industries else 0,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    base = StrategyBacktestConfig(
        strategy_id=req.strategy_ids[0],
        symbols=req.symbols,
        start=start,
        end=end,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
        max_positions=req.max_positions,
        initial_capital=req.initial_capital,
        mode="position",
        risk_free_rate=req.risk_free_rate,
    )
    search_ids = list(dict.fromkeys(req.strategy_ids))
    strategy_labels = {sid: sid for sid in search_ids}
    if req.include_combos:
        combo_specs = expand_combo_specs(leaf_strategy_ids(strategy_engine, search_ids))
        try:
            installed = install_combo_strategies(strategy_engine, combo_specs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for combo_id, _children, _mode, label in combo_specs:
            if combo_id in installed:
                search_ids.append(combo_id)
                strategy_labels[combo_id] = label
    try:
        pg_expanded = _validate_and_expand_param_grid(
            strategy_engine, req.param_grid, strategy_ids=search_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        scenarios, requested, truncated = expand_search_scenarios(
            strategy_ids=search_ids,
            universes=universes,
            holding_days=req.holding_days,
            matchings=list(req.matchings),
            base=base,
            max_scenarios=req.max_scenarios,
            strategy_labels=strategy_labels,
            param_grid_expanded=pg_expanded,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


    config_hash = compute_config_hash({
        "strategy_ids": search_ids,
        "include_combos": req.include_combos,
        "universes": [u.universe_id for u in universes],
        "holding_days": req.holding_days,
        "matchings": req.matchings,
        "start": start,
        "end": end,
        "train_end": train_end,
        "objective": req.objective,
        "min_trades": req.min_trades,
        "max_drawdown": req.max_drawdown,
        "max_scenarios": req.max_scenarios,
        "param_grid": req.param_grid or {},
    })
    experiment_id = f"so-{uuid.uuid4().hex[:12]}"
    _cleanup_stale_jobs()
    with _jobs_lock:
        existing = _find_running(config_hash)
        if existing is not None:
            return {
                "experiment_id": existing.experiment_id,
                "config_hash": config_hash,
                "scenario_count": len(scenarios),
                "requested_count": requested,
                "truncated": truncated,
                "objective": req.objective,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "train_end": train_end.isoformat(),
                "holdout_start": holdout_start.isoformat(),
                "status": "already_running",
            }

    svc = StrategyBacktestService(engine, strategy_engine)
    store = _get_store(request)
    with _jobs_lock:
        job = _OptJob(experiment_id, config_hash)
        _jobs[experiment_id] = job

    def _progress_cb(evt: dict) -> None:
        job.progress.append(evt)
        try:
            job.snapshot = store.load(experiment_id).to_dict()
        except Exception:  # noqa: BLE001
            pass

    def _run() -> None:
        try:
            exp = run_search(
                svc,
                store,
                experiment_id=experiment_id,
                config_hash=config_hash,
                objective=req.objective,
                scenarios=scenarios,
                requested_count=requested,
                truncated=truncated,
                train_start=train_start,
                train_end=train_end,
                holdout_start=holdout_start,
                holdout_end=holdout_end,
                min_trades=req.min_trades,
                max_drawdown=req.max_drawdown,
                top_k=req.top_k,
                extra_warnings=window_warnings,
                progress_cb=_progress_cb,
                cancel_event=job.cancel_event,
                param_grid=req.param_grid,
            )
            job.snapshot = exp.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.exception("optimizer experiment failed")
            job.error = str(exc)
        finally:
            job.done = True
            job.finish_ts = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return {
        "experiment_id": experiment_id,
        "config_hash": config_hash,
        "scenario_count": len(scenarios),
        "requested_count": requested,
        "truncated": truncated,
        "objective": req.objective,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "train_end": train_end.isoformat(),
        "holdout_start": holdout_start.isoformat(),
        "status": "started",
    }


@router.get("/optimizer/{experiment_id}")
def optimizer_detail(experiment_id: str, request: Request):
    job = _jobs.get(experiment_id)
    if job is not None and job.snapshot is not None:
        return job.snapshot
    try:
        return _get_store(request).load(experiment_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/optimizer/{experiment_id}/stream")
async def optimizer_stream(experiment_id: str, request: Request):
    job = _jobs.get(experiment_id)

    async def event_generator():
        if job is None:
            try:
                exp = _get_store(request).load(experiment_id)
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
                tick += 1
                if tick % 4 == 0 and await request.is_disconnected():
                    break
                while cursor < len(job.progress):
                    msg = job.progress[cursor]
                    cursor += 1
                    yield f"event: progress\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/optimizer/{experiment_id}/cancel")
async def optimizer_cancel(experiment_id: str):
    job = _jobs.get(experiment_id)
    if job is not None and not job.done:
        job.cancel_event.set()
        return {"ok": True, "experiment_id": experiment_id}
    return {"ok": False, "message": "实验不存在或已完成"}
