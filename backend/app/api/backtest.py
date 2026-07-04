"""回测 API — 信号回测 + 因子回测 + 策略回测。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.services.backtest import (
    BacktestConfig,
    BacktestService,
    VectorbtUnavailable,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])
logger = logging.getLogger(__name__)

FACTOR_DEFAULT_DAYS = 180
STRATEGY_DEFAULT_DAYS = 365 * 3
BACKTEST_MAX_SERVER_DAYS = 186
FACTOR_MAX_SYMBOLS = 1000
BACKTEST_SERVER_GUARD_MESSAGE = (
    "当前服务器内存约 1.8GB，回测区间最多支持 6 个月；"
    "更长周期容易触发 OOM，建议在 8GB 以上内存环境或本机运行。"
)


def _get_engine(request: Request):
    """获取或创建 BacktestEngine (单例，PanelCache 跨请求生效)。"""
    from app.backtest.engine import BacktestEngine
    engine = getattr(request.app.state, "backtest_engine", None)
    if engine is None:
        engine = BacktestEngine(request.app.state.repo)
        request.app.state.backtest_engine = engine
    return engine


def _resolve_start(req: BaseModel, end: date, default_days: int) -> date:
    """未传 start 使用默认区间；显式传 null/空值表示全部历史。"""
    start = getattr(req, "start")
    if start is not None:
        return start
    if "start" in req.model_fields_set:
        return date(1900, 1, 1)
    return end - timedelta(days=default_days)


def _guard_server_backtest_range(start: date, end: date):
    if not settings.backtest_range_guard:
        return
    days = (end - start).days + 1
    if days > BACKTEST_MAX_SERVER_DAYS:
        raise HTTPException(status_code=400, detail=BACKTEST_SERVER_GUARD_MESSAGE)


def _attach_methodology(payload: dict, scenario: str = "backtest") -> dict:
    warnings = payload.setdefault("warnings", [])
    from app.services.skill_context import load_skill_context_safe

    methodology_context = load_skill_context_safe(scenario, max_chars=4000, warnings=warnings)
    if methodology_context:
        payload["methodology_context"] = methodology_context
    return payload


# ================================================================
# 状态
# ================================================================

@router.get("/status")
def status():
    """前端可用此接口判断回测页是否要灰显。"""
    return {"available": True}


# ================================================================
# 信号回测 (现有接口，保持不变)
# ================================================================

class BacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    start: date | None = None
    end: date | None = None
    entries: list[str] = []
    exits: list[str] = []
    stop_loss_pct: float | None = None
    max_hold_days: int | None = None
    fees_pct: float = 0.0002
    slippage_bps: float = 5
    matching: Literal["close_t", "open_t+1"] = "close_t"


@router.post("/run")
def run(req: BacktestRequest, request: Request):
    """信号回测 — 现有接口，向后兼容。"""
    repo = request.app.state.repo
    svc = BacktestService(repo)
    end = req.end or date.today()
    start = req.start or (end - timedelta(days=365 * 3))

    cfg = BacktestConfig(
        symbols=req.symbols,
        start=start,
        end=end,
        entries=req.entries,
        exits=req.exits,
        stop_loss_pct=req.stop_loss_pct,
        max_hold_days=req.max_hold_days,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
        matching=req.matching,
    )
    try:
        result = svc.run(cfg)
    except VectorbtUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return asdict(result)


# ================================================================
# 组合优化器 (P3)
# ================================================================

class OptimizeRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    method: Literal[
        "equal", "equal_vol", "risk_parity",
        "mean_variance", "max_diversification", "score_weight",
    ] = "risk_parity"
    lookback_days: int = Field(120, ge=20, le=1000)


@router.post("/optimize")
def optimize(req: OptimizeRequest, request: Request):
    """组合优化器：给一组标的算配置权重（支持 A股 + ETF）。"""
    import numpy as np

    from app.backtest.optimizers import portfolio_weights
    from app.backtest.portfolio import load_price_matrix, momentum_from_prices, returns_from_prices

    repo = request.app.state.repo
    end = date.today()
    start = end - timedelta(days=req.lookback_days)

    prices, kept = load_price_matrix(repo, req.symbols, start, end)
    if len(kept) < 2:
        raise HTTPException(status_code=400, detail="有效标的不足 2 只（数据缺失、港股或标的过少）")
    if prices.shape[0] < 2:
        raise HTTPException(status_code=400, detail="标的间共同交易日不足，无法估计收益/协方差")

    rets = returns_from_prices(prices)
    scores = momentum_from_prices(prices) if req.method == "score_weight" else None
    weights_arr = np.asarray(portfolio_weights(rets, req.method, scores), dtype=float)

    stats = {"n": len(kept), "annualized_vol": None, "diversification_ratio": None}
    clean = rets[np.isfinite(rets).all(axis=1)] if rets.size else rets
    if clean.shape[0] >= 2:
        cov = np.atleast_2d(np.cov(clean, rowvar=False))
        port_vol = float(np.sqrt(max(float(weights_arr @ cov @ weights_arr), 0.0)))
        vol = np.sqrt(np.maximum(np.diag(cov), 1e-12))
        stats["annualized_vol"] = round(port_vol * float(np.sqrt(252)), 6)
        if port_vol > 0:
            stats["diversification_ratio"] = round(float(weights_arr @ vol) / port_vol, 4)

    name_map: dict[str, str] = {}
    try:
        inst = repo.get_instruments()
        if inst is not None and not inst.is_empty() and {"symbol", "name"} <= set(inst.columns):
            name_map = dict(zip(inst["symbol"].to_list(), inst["name"].to_list()))
    except Exception:  # noqa: BLE001
        pass

    dropped = [symbol for symbol in req.symbols if symbol not in kept]
    weights = [
        {"symbol": symbol, "name": name_map.get(symbol), "weight": round(float(weights_arr[i]), 6)}
        for i, symbol in enumerate(kept)
    ]
    return {
        "weights": weights,
        "stats": stats,
        "method": req.method,
        "lookback_days": req.lookback_days,
        "meta": {"kept": kept, "dropped": dropped},
    }


# ================================================================
# 因子回测
# ================================================================

class FactorColumnsResponse(BaseModel):
    columns: list[dict]


@router.get("/factor/columns")
def factor_columns():
    """返回可用的因子列列表。"""
    from app.backtest.factor import FACTOR_COLUMNS
    return {"columns": FACTOR_COLUMNS}


@router.get("/factors/manifest")
def factor_manifest():
    """返回 Alpha Zoo metadata；不触发行情读取或因子计算。"""
    from app.backtest.factor_zoo import export_manifest

    return {"factors": export_manifest()}


class FactorBacktestRequest(BaseModel):
    factor_name: str
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    n_groups: int = 5
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0


class FactorCompareRequest(BaseModel):
    factor_ids: list[str] = Field(..., min_length=1, max_length=20)
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    universe: str | None = None
    strict: bool = False


@router.post("/factor/run")
def factor_run(req: FactorBacktestRequest, request: Request):
    """因子回测 — IC/IR 分析 + 分层回测。"""
    from app.backtest.factor import FactorBacktestService, FactorConfig

    engine = _get_engine(request)
    svc = FactorBacktestService(engine)

    end = req.end or date.today()
    start = _resolve_start(req, end, STRATEGY_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)
    symbols = req.symbols if req.symbols else None
    if symbols is not None and len(symbols) > FACTOR_MAX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"指定标的最多支持 {FACTOR_MAX_SYMBOLS} 只，请缩小标的范围。",
        )

    cfg = FactorConfig(
        factor_name=req.factor_name,
        symbols=symbols,
        start=start,
        end=end,
        n_groups=req.n_groups,
        rebalance=req.rebalance,
        weight=req.weight,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
    )
    result = svc.run(cfg)
    return asdict(result)


@router.post("/factors/compare")
def factor_compare(req: FactorCompareRequest, request: Request):
    """批量比较 Alpha Zoo 因子。复用现有因子回测服务，避免第二套 IC 计算。"""
    from app.backtest.factor import FactorBacktestService, FactorConfig
    from app.backtest.factor_zoo import ALPHAS

    unknown = [x for x in req.factor_ids if x not in ALPHAS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown factor: {unknown[0]}")

    engine = _get_engine(request)
    svc = FactorBacktestService(engine)
    end = req.end or date.today()
    start = _resolve_start(req, end, STRATEGY_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)
    out = []
    for factor_id in req.factor_ids:
        result = svc.run(FactorConfig(
            factor_name=factor_id,
            symbols=req.symbols if req.symbols else None,
            start=start,
            end=end,
        ))
        row = {
            "factor_id": factor_id,
            "coverage": result.n_symbols,
            "n_dates": result.n_dates,
            "ic_mean": result.ic_mean,
            "ic_ir": result.ir,
            "rank_ic_mean": result.ic_mean,
            "error": result.error,
        }
        if req.strict:
            random_control = svc.random_control_ic(FactorConfig(
                factor_name=factor_id,
                symbols=req.symbols if req.symbols else None,
                start=start,
                end=end,
            ))
            row.update(random_control)
            row["delta_vs_random"] = (
                round(float(result.ic_mean - random_control["random_control_ic_mean"]), 4)
                if result.ic_mean is not None and random_control["random_control_ic_mean"] is not None
                else None
            )
        out.append(row)
    return {"factors": out}


def _save_strategy_run_card(request: Request, result) -> None:
    try:
        from app.services.research_registry import ResearchStore

        data_dir = request.app.state.repo.store.data_dir
        ResearchStore(data_dir).save_run_card(
            run_id=result.run_id,
            kind="strategy",
            config=result.config,
            strategy_def=result.strategy_info,
            stats=result.stats,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("save strategy run_card failed: %s", e)


def _strategy_stream_done_event(request: Request, result) -> str:
    if hasattr(result, "error") and result.error == "cancelled":
        return f"event: error\ndata: {json.dumps({'message': '回测已取消'}, ensure_ascii=False)}\n\n"
    if hasattr(result, "error") and result.error:
        return f"event: error\ndata: {json.dumps({'message': result.error}, ensure_ascii=False)}\n\n"
    _save_strategy_run_card(request, result)
    return f"event: done\ndata: {json.dumps(asdict(result), ensure_ascii=False, default=str)}\n\n"


def _walk_forward_windows(start: date, end: date, n_folds: int) -> list[tuple[date, date]]:
    n_folds = max(1, min(12, int(n_folds)))
    total = (end - start).days + 1
    fold_len = total // n_folds
    if fold_len < 30:
        raise ValueError(f"窗口过短: {n_folds} 窗每窗仅 {fold_len} 天(<30)")
    out = []
    cur = start
    for i in range(n_folds):
        fold_end = end if i == n_folds - 1 else cur + timedelta(days=fold_len - 1)
        out.append((cur, fold_end))
        cur = fold_end + timedelta(days=1)
    return out


# ================================================================
# 策略回测
# ================================================================

class StrategyBacktestRequest(BaseModel):
    strategy_id: str
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    params: dict | None = None
    overrides: dict | None = None
    # matching 向后兼容; 显式传 entry_fill/exit_fill 时以二者为准。
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


@router.post("/strategy/run")
def strategy_run(req: StrategyBacktestRequest, request: Request):
    """策略回测 — 复用 StrategyDef 体系做全周期回测。"""
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

    engine = _get_engine(request)
    strategy_engine = request.app.state.strategy_engine
    svc = StrategyBacktestService(engine, strategy_engine)

    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    cfg = StrategyBacktestConfig(
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
    )
    result = svc.run(cfg)
    _save_strategy_run_card(request, result)
    return _attach_methodology(asdict(result), "backtest")


class RobustnessRequest(StrategyBacktestRequest):
    n_folds: int = 4
    bootstrap: bool = True
    mc_permutation: bool = False
    n_boot: int = 1000
    n_perm: int = 1000


@router.post("/strategy/robustness")
def strategy_robustness(req: RobustnessRequest, request: Request):
    from app.backtest import robustness as rb
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
    from app.services.research_registry import ResearchStore

    engine = _get_engine(request)
    svc = StrategyBacktestService(engine, request.app.state.strategy_engine)
    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)
    try:
        windows = _walk_forward_windows(start, end, req.n_folds)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    def run_one(s: date, e: date):
        return svc.run(StrategyBacktestConfig(
            strategy_id=req.strategy_id,
            symbols=req.symbols if req.symbols else None,
            start=s,
            end=e,
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
        ))

    full = run_one(start, end)
    if full.error:
        raise HTTPException(status_code=400, detail=full.error)
    folds = []
    for s, e in windows:
        r = run_one(s, e)
        folds.append({"start": s.isoformat(), "end": e.isoformat(), "stats": r.stats, "error": r.error})

    rets = rb.returns_from_equity_curve(full.equity_curve)
    robustness = {
        "walk_forward": {
            "folds": folds,
            "summary": rb.walk_forward_summary([f for f in folds if not f["error"]]),
        },
        "exit_breakdown": rb.exit_reason_breakdown(full.trades),
    }
    if req.bootstrap and len(rets) >= 2:
        robustness["bootstrap"] = rb.bootstrap_sharpe_ci(rets, n_boot=req.n_boot)
    if req.mc_permutation and len(rets) >= 2:
        robustness["mc_permutation"] = rb.mc_permutation_pvalue(rets, n_perm=req.n_perm)

    try:
        ResearchStore(request.app.state.repo.store.data_dir).save_run_card(
            run_id=full.run_id,
            kind="strategy",
            config=full.config,
            strategy_def=full.strategy_info,
            stats={**full.stats, "robustness": robustness},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("save robustness run_card failed: %s", e)

    return _attach_methodology({"run_id": full.run_id, "full_stats": full.stats, **robustness}, "backtest")


class _BacktestJob:
    """单个回测任务的状态, 存模块级供重连使用。"""
    __slots__ = ("key", "cancel_event", "progress", "result", "error", "done", "finish_ts")

    def __init__(self, key: str):
        self.key = key
        self.cancel_event = threading.Event()
        self.progress: list[dict] = []   # 进度历史 (新连接可回放)
        self.result = None               # 完成后的结果
        self.error: str | None = None
        self.done = False
        self.finish_ts: float = 0.0


# 模块级任务表: key -> _BacktestJob
_running_jobs: dict[str, _BacktestJob] = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 300  # 完成后保留 5 分钟


def _cleanup_stale_jobs():
    """清理过期任务 (完成超过 TTL 的)。"""
    now = time.time()
    stale = [k for k, j in _running_jobs.items() if j.done and now - j.finish_ts > _JOB_TTL]
    for k in stale:
        _running_jobs.pop(k, None)


def _make_job_key(
    strategy_id: str, symbols: str | None, start: str | None, end: str | None,
    matching: str, entry_fill: str | None, exit_fill: str | None,
    fees_pct: float, slippage_bps: float,
    max_positions: int, max_exposure_pct: float, initial_capital: float, position_sizing: str,
    params: str | None, overrides: str | None,
    mode: str = "position", holding_days: int = 5,
) -> str:
    raw = f"{strategy_id}|{symbols}|{start}|{end}|{matching}|{entry_fill}|{exit_fill}|{fees_pct}|{slippage_bps}|{max_positions}|{max_exposure_pct}|{initial_capital}|{position_sizing}|{params}|{overrides}|{mode}|{holding_days}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


@router.get("/strategy/stream")
async def strategy_stream(
    request: Request,
    strategy_id: str,
    symbols: str | None = None,
    start: str | None = None,
    end: str | None = None,
    matching: str = "open_t+1",
    entry_fill: str | None = None,
    exit_fill: str | None = None,
    fees_pct: float = 0.0002,
    slippage_bps: float = 5.0,
    max_positions: int = 10,
    max_exposure_pct: float = 1.0,
    initial_capital: float = 1_000_000.0,
    position_sizing: str = "equal",
    params: str | None = None,
    overrides: str | None = None,
    mode: str = "position",
    holding_days: int = 5,
):
    """SSE 流式策略回测: 实时推送进度, 完成后推送结果, 支持重连 (刷新/切页后恢复)。

    - 相同参数的任务只启动一次, 多次连接订阅同一个任务
    - 断开连接不会取消任务 (除非显式调用 cancel)
    - 结果保留 5 分钟供重连

    事件类型:
      - progress: {day, total, date, equity}
      - done: {result} (完整回测结果)
      - error: {message}
    """
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

    engine = _get_engine(request)
    strategy_engine = request.app.state.strategy_engine
    svc = StrategyBacktestService(engine, strategy_engine)

    end_date = date.fromisoformat(end) if end else date.today()
    if start:
        start_date = date.fromisoformat(start)
    else:
        # 空 start = 全部历史: 用本地最早日K日期, 查不到再回退到默认窗口
        earliest = request.app.state.repo.earliest_daily_date()
        start_date = earliest or (end_date - timedelta(days=FACTOR_DEFAULT_DAYS))

    # 服务端范围保护
    guard_violated = False
    if settings.backtest_range_guard:
        days = (end_date - start_date).days + 1
        if days > BACKTEST_MAX_SERVER_DAYS:
            guard_violated = True

    job_key = _make_job_key(
        strategy_id, symbols, start, end,
        matching, entry_fill, exit_fill,
        fees_pct, slippage_bps, max_positions, max_exposure_pct, initial_capital, position_sizing,
        params, overrides,
        mode, holding_days,
    )

    _cleanup_stale_jobs()

    # 获取或创建任务
    with _jobs_lock:
        job = _running_jobs.get(job_key)
        if job is None:
            job = _BacktestJob(job_key)
            _running_jobs[job_key] = job
            is_new = True
        else:
            is_new = False

    async def event_generator():
        # 范围保护: 直接报错
        if guard_violated:
            yield f"event: error\ndata: {json.dumps({'message': BACKTEST_SERVER_GUARD_MESSAGE}, ensure_ascii=False)}\n\n"
            return

        # 如果是新任务, 启动回测线程
        if is_new and not job.done:
            cfg = StrategyBacktestConfig(
                strategy_id=strategy_id,
                symbols=[s.strip() for s in symbols.split(",") if s.strip()] if symbols else None,
                start=start_date,
                end=end_date,
                params=json.loads(params) if params else None,
                overrides=json.loads(overrides) if overrides else None,
                matching=matching,
                entry_fill=entry_fill,
                exit_fill=exit_fill,
                fees_pct=fees_pct,
                slippage_bps=slippage_bps,
                max_positions=int(max_positions),
                max_exposure_pct=float(max_exposure_pct),
                initial_capital=float(initial_capital),
                position_sizing=position_sizing,
                mode=mode,
                holding_days=int(holding_days),
            )

            def _run_backtest():
                try:
                    result = svc.run(cfg, lambda d: job.progress.append(d), job.cancel_event)
                    job.result = result
                    job.done = True
                    job.finish_ts = time.time()
                except Exception as e:
                    job.error = str(e)
                    job.done = True
                    job.finish_ts = time.time()

            # 启动后台线程 (不阻塞事件循环)
            threading.Thread(target=_run_backtest, daemon=True).start()

        # 订阅进度: 用读指针读 job.progress 列表 (多连接互不干扰)
        cursor = 0
        tick = 0

        try:
            while True:
                # 已完成: 推送最终结果/错误并退出
                if job.done:
                    if job.error:
                        yield f"event: error\ndata: {json.dumps({'message': job.error}, ensure_ascii=False)}\n\n"
                    elif job.result is not None:
                        yield _strategy_stream_done_event(request, job.result)
                    return

                # 断开检测: 每 4 轮检查一次 (降低 GIL 抢占频率)
                tick += 1
                if tick % 4 == 0 and await request.is_disconnected():
                    break

                # 推送新进度 (从 cursor 开始读)
                prog_list = job.progress
                while cursor < len(prog_list):
                    msg = prog_list[cursor]
                    cursor += 1
                    yield f"event: progress\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/strategy/cancel")
async def strategy_cancel(request: Request):
    """取消正在运行的回测任务 (前端传 query string, 后端算 job_key)。"""
    body = await request.json()
    qs = body.get("qs", "")
    # 解析 qs 得到参数
    from urllib.parse import parse_qs
    p = parse_qs(qs)
    def _get(key: str, default: str = "") -> str:
        return p.get(key, [default])[0]
    job_key = _make_job_key(
        _get("strategy_id"),
        _get("symbols") or None,
        _get("start") or None,
        _get("end") or None,
        _get("matching", "open_t+1"),
        _get("entry_fill") or None,
        _get("exit_fill") or None,
        float(_get("fees_pct", "0.0002")),
        float(_get("slippage_bps", "5")),
        int(_get("max_positions", "10")),
        float(_get("max_exposure_pct", "1")),
        float(_get("initial_capital", "1000000")),
        _get("position_sizing", "equal"),
        _get("params") or None,
        _get("overrides") or None,
        _get("mode", "position"),
        int(_get("holding_days", "5")),
    )
    job = _running_jobs.get(job_key)
    if job and not job.done:
        job.cancel_event.set()
        return {"ok": True}
    return {"ok": False, "message": "任务不存在或已完成"}
