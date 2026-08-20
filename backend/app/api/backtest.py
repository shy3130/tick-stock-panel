"""回测 API — 信号回测 + 因子回测 + 策略回测。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.backtest.metrics import MetricContext
from app.backtest.provenance import build_run_provenance
from app.backtest.run_store import (
    RUN_KINDS,
    BacktestRun,
    BacktestRunStore,
    LegacyRunCardReadOnly,
    RunIdError,
    RunTooLargeError,
    compare_runs,
    export_csv,
)
from app.backtest.run_store import RunSubject as BacktestRunSubject
from app.config import settings
from app.json_safe import json_safe

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

def _derive_random_seed(payload: dict, data_version: str | None) -> int:
    """从冻结请求和数据版本派生可复现的 63 位随机种子。"""
    encoded = json.dumps(
        {"config": payload, "data_version": data_version},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") & ((1 << 63) - 1)


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

# 实际收益/净值序列的优先级: 策略 equity_curve 在前, 因子回退 group_nav/long_short_nav,
# IC-only run 最后用 ic_series。均无可用序列时 provenance 回退请求区间。
OBSERVED_SERIES_KEYS = ("equity_curve", "group_nav", "long_short_nav", "ic_series")


def _series_date_bounds(series) -> tuple[date, date] | None:
    """单条序列内可解析 ISO 日期的范围; 非法/缺失日期忽略, 无有效日期返回 None。"""
    dates: list[date] = []
    for row in series or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("date")
        if raw is None:
            continue
        try:
            dates.append(date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
    if not dates:
        return None
    return min(dates), max(dates)


def _observed_coverage(payload: dict, start: date, end: date) -> tuple[date, date]:
    """指标实际使用的观测覆盖: 序列日期钳制在请求区间内, 旧 payload/异常序列回退请求区间。"""
    for key in OBSERVED_SERIES_KEYS:
        bounds = _series_date_bounds(payload.get(key))
        if bounds is None:
            continue
        lo, hi = bounds
        lo, hi = max(lo, start), min(hi, end)
        if lo <= hi:
            return lo, hi
    return start, end


def _attach_run_provenance(
    payload: dict,
    request: Request,
    *,
    start: date,
    end: date,
    symbols: list[str] | None,
    return_frequency: Literal["daily", "weekly", "monthly"] = "daily",
    random_seed: int | None = None,
    warnings: list[str] | None = None,
    risk_free_rate: float | None = None,
    coverage: tuple[date, date] | None = None,
) -> dict:
    """附加 provenance。coverage 为调用方已确定的实际观测覆盖 (如 robustness
    响应无观测序列时由 full.equity_curve 提取), 未提供时从 payload 序列推导。"""
    if risk_free_rate is None:
        config = payload.get("config")
        if isinstance(config, dict):
            try:
                risk_free_rate = float(config.get("risk_free_rate", 0.0))
            except (TypeError, ValueError):
                risk_free_rate = 0.0
    if risk_free_rate is None:
        risk_free_rate = 0.0
    coverage_start, coverage_end = (
        coverage if coverage is not None else _observed_coverage(payload, start, end)
    )
    stats = payload.get("stats")
    is_candidate_execution = (
        isinstance(stats, dict) and stats.get("full_kind") == "candidate_execution"
    )
    provenance = build_run_provenance(
        request.app.state.repo,
        start=coverage_start,
        end=coverage_end,
        symbols=symbols,
        metric_context=MetricContext(return_frequency, risk_free_rate=risk_free_rate),
        random_seed=random_seed,
    )
    if is_candidate_execution:
        # 独立候选收益按退出事件采样，不能声称它具有日频 MetricContext。
        provenance.pop("metric_context", None)
    existing = list(payload.get("warnings") or [])
    candidate_warning = ["candidate_return_curve"] if is_candidate_execution else []
    merged_warnings = [
        *existing,
        *provenance.pop("warnings"),
        *candidate_warning,
        *(warnings or []),
    ]
    payload.update(provenance)
    payload["warnings"] = list(dict.fromkeys(merged_warnings))
    return payload


def _run_store(request: Request) -> BacktestRunStore:
    return BacktestRunStore(request.app.state.repo.store.data_dir)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _run_from_strategy_payload(
    payload: dict,
    *,
    kind: str,
    random_seed: int | None = None,
) -> BacktestRun:
    """从已附加 provenance 的策略回测响应构造不可变 BacktestRun。"""
    cfg = payload.get("config") or {}
    strategy_info = payload.get("strategy_info") or {}
    subject = BacktestRunSubject(
        id=str(strategy_info.get("id") or cfg.get("strategy_id") or payload.get("run_id") or ""),
        name=str(strategy_info.get("name") or cfg.get("strategy_id") or ""),
        hash=_subject_hash(strategy_info, cfg),
    )
    return BacktestRun(
        run_id=str(payload.get("run_id") or ""),
        kind=kind,
        created_at=_utc_now_iso(),
        status="completed" if not payload.get("error") else "failed",
        subject=subject,
        config=cfg,
        data_snapshot=payload.get("data_snapshot") or {},
        benchmark={
            "symbol": cfg.get("benchmark_symbol", "000001.INDEX"),
            "name": (
                (payload.get("benchmark_curve") or [{}])[0].get("name")
                if payload.get("benchmark_curve")
                else None
            ),
        },
        cost_model={
            k: cfg.get(k)
            for k in ("fees_pct", "slippage_bps", "entry_fill", "exit_fill", "matching")
            if cfg.get(k) is not None
        },
        metric_context=payload.get("metric_context") or {},
        random_seed=random_seed if random_seed is not None else payload.get("random_seed"),
        engine_version=payload.get("engine_version") or "",
        stats=payload.get("stats") or {},
        equity_curve=payload.get("equity_curve") or [],
        drawdown_curve=payload.get("drawdown_curve") or [],
        benchmark_curve=payload.get("benchmark_curve") or [],
        trades=payload.get("trades") or [],
        per_symbol_stats=payload.get("per_symbol_stats") or [],
        attribution=payload.get("attribution"),
        warnings=list(payload.get("warnings") or []),
    )


def _run_from_factor_payload(payload: dict) -> BacktestRun:
    """因子回测响应 → BacktestRun (曲线放 factor_result, 不占用 equity_curve)。"""
    cfg = payload.get("config") or {}
    factor_result = {
        k: payload.get(k)
        for k in (
            "ic_mean", "ic_std", "ir", "ic_win_rate", "ic_series", "group_stats",
            "group_nav", "group_turnover", "long_short_nav", "long_short_stats",
            "n_symbols", "n_dates",
        )
        if k in payload
    }
    return BacktestRun(
        run_id=str(payload.get("run_id") or ""),
        kind="factor",
        created_at=_utc_now_iso(),
        status="completed" if not payload.get("error") else "failed",
        subject=BacktestRunSubject(
            id=str(cfg.get("factor_name") or payload.get("run_id") or ""),
            name=str(cfg.get("factor_name") or ""),
            hash=_stable_hex(cfg),
        ),
        config=cfg,
        data_snapshot=payload.get("data_snapshot") or {},
        cost_model={
            k: cfg.get(k)
            for k in ("fees_pct", "slippage_bps")
            if cfg.get(k) is not None
        },
        metric_context=payload.get("metric_context") or {},
        random_seed=payload.get("random_seed"),
        engine_version=payload.get("engine_version") or "",
        stats={},
        factor_result=factor_result,
        warnings=list(payload.get("warnings") or []),
    )


def _save_backtest_run(request: Request, run: BacktestRun) -> BacktestRun | None:
    """落盘不可变 Run；SSE 重连重复保存时返回已存在的同一事实。"""
    store = _run_store(request)
    try:
        return store.save(run)
    except FileExistsError:
        try:
            existing = store.get(run.run_id)
        except KeyError:
            return None
        existing_payload = json_safe(existing.model_dump())
        incoming_payload = json_safe(run.model_dump())
        # SSE 多订阅者会为同一不可变事实重复组装 Run；created_at 是组装时刻，
        # favorite/label 是契约内可变元数据 (可能已被用户 PATCH)，均不属于
        # 事实身份。除这些字段外仍要求逐字段一致，真正的 run_id 冲突继续拒绝。
        for volatile_field in ("created_at", "favorite", "label"):
            existing_payload.pop(volatile_field, None)
            incoming_payload.pop(volatile_field, None)
        if existing_payload != incoming_payload:
            logger.error("BacktestRun id collision with different payload: %s", run.run_id)
            return None
        return existing
    except (RunTooLargeError, RunIdError, OSError) as e:
        logger.warning("save BacktestRun %s failed: %s", run.run_id, e)
        return None


def _persist_backtest_run(request: Request, run: BacktestRun, response_payload: dict) -> bool:
    """保存完整 Run；失败必须反馈给调用方，不能伪装为已进入运行历史。"""
    if _save_backtest_run(request, run) is not None:
        return True
    warnings = response_payload.setdefault("warnings", [])
    warning = "persistence_failed: 回测已完成，但完整运行记录未能写入运行历史"
    if warning not in warnings:
        warnings.append(warning)
    return False


def _strategy_run_kind(strategy_info: dict) -> str:
    """composite 由 strategy_info.source 判别。"""
    return "composite" if (strategy_info or {}).get("source") == "composite" else "strategy"


def _subject_hash(strategy_info: dict, cfg: dict) -> str:
    import hashlib

    raw = json.dumps(
        {"meta": strategy_info.get("meta"), "id": strategy_info.get("id"), "cfg_params": cfg.get("params")},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _stable_hex(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


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
    payload = _attach_run_provenance(
        json_safe(asdict(result)),
        request,
        start=start,
        end=end,
        symbols=req.symbols,
        warnings=[
            "legacy_vectorbt_engine: 旧信号回测与主 Polars/NumPy 引擎语义不同，结果不可直接横向比较",
        ],
    )
    return _attach_methodology(payload, "backtest")


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
    return json_safe({
        "weights": weights,
        "stats": stats,
        "method": req.method,
        "lookback_days": req.lookback_days,
        "meta": {"kept": kept, "dropped": dropped},
    })


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
    n_groups: int = Field(default=5, ge=2, le=10)
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    risk_free_rate: float = Field(default=0.0, gt=-1.0, le=1.0)

def _build_factor_config(
    *,
    factor_name: str,
    symbols: list[str] | None,
    start: date | None,
    end: date | None,
    start_is_all_history: bool,
    n_groups: int,
    rebalance: Literal["daily", "weekly", "monthly"],
    weight: Literal["equal", "factor_weight"],
    fees_pct: float,
    slippage_bps: float,
    risk_free_rate: float,
):
    """规范化同步/SSE 因子回测的同一份配置，避免两条入口口径漂移。"""
    from app.backtest.factor import FactorConfig

    effective_end = end or date.today()
    effective_start = (
        date(1900, 1, 1)
        if start is None and start_is_all_history
        else start or (effective_end - timedelta(days=FACTOR_DEFAULT_DAYS))
    )
    effective_symbols = symbols if symbols else None
    if effective_symbols is not None and len(effective_symbols) > FACTOR_MAX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"指定标的最多支持 {FACTOR_MAX_SYMBOLS} 只，请缩小标的范围。",
        )
    return FactorConfig(
        factor_name=factor_name,
        symbols=effective_symbols,
        start=effective_start,
        end=effective_end,
        n_groups=n_groups,
        rebalance=rebalance,
        weight=weight,
        fees_pct=fees_pct,
        slippage_bps=slippage_bps,
        risk_free_rate=risk_free_rate,
    )



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
    from app.backtest.factor import FactorBacktestService

    cfg = _build_factor_config(
        factor_name=req.factor_name,
        symbols=req.symbols,
        start=req.start,
        end=req.end,
        start_is_all_history=req.start is None and "start" in req.model_fields_set,
        n_groups=req.n_groups,
        rebalance=req.rebalance,
        weight=req.weight,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
        risk_free_rate=req.risk_free_rate,
    )
    _guard_server_backtest_range(cfg.start, cfg.end)
    engine = _get_engine(request)
    svc = FactorBacktestService(engine)
    result = svc.run(cfg)
    payload = _attach_run_provenance(
        json_safe(asdict(result)),
        request,
        start=cfg.start,
        end=cfg.end,
        symbols=cfg.symbols,
        return_frequency=cfg.rebalance,
    )
    if not payload.get("error"):
        payload["persisted"] = _persist_backtest_run(
            request,
            _run_from_factor_payload(payload),
            payload,
        )
    return _attach_methodology(payload, "backtest")


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
    return json_safe({"factors": out})


def _strategy_stream_done_event(request: Request, result) -> str:
    if hasattr(result, "error") and result.error == "cancelled":
        return f"event: error\ndata: {json.dumps({'message': '回测已取消'}, ensure_ascii=False)}\n\n"
    if hasattr(result, "error") and result.error:
        return f"event: error\ndata: {json.dumps({'message': result.error}, ensure_ascii=False)}\n\n"
    payload = json_safe(asdict(result))
    config = result.config or {}
    payload = _attach_run_provenance(
        payload,
        request,
        start=date.fromisoformat(str(config["start"])),
        end=date.fromisoformat(str(config["end"])),
        symbols=config.get("symbols"),
    )
    # BacktestRun 是策略回测的唯一持久化契约；旧 run_card 仅只读迁移。
    payload["persisted"] = _persist_backtest_run(
        request,
        _run_from_strategy_payload(
            payload,
            kind=_strategy_run_kind(payload.get("strategy_info") or {}),
        ),
        payload,
    )
    payload = _attach_methodology(payload, "backtest")
    return f"event: done\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)}\n\n"

def _factor_stream_done_event(request: Request, result) -> str:
    """把完成的因子任务收敛为与同步端点相同的持久化响应。"""
    if getattr(result, "error", None) == "cancelled":
        return f"event: error\ndata: {json.dumps({'message': '因子回测已取消'}, ensure_ascii=False)}\n\n"
    if getattr(result, "error", None):
        return f"event: error\ndata: {json.dumps({'message': result.error}, ensure_ascii=False)}\n\n"
    payload = json_safe(asdict(result))
    config = result.config or {}
    payload = _attach_run_provenance(
        payload,
        request,
        start=date.fromisoformat(str(config["start"])),
        end=date.fromisoformat(str(config["end"])),
        symbols=config.get("symbols"),
        return_frequency=config.get("rebalance", "monthly"),
    )
    payload["persisted"] = _persist_backtest_run(
        request,
        _run_from_factor_payload(payload),
        payload,
    )
    payload = _attach_methodology(payload, "backtest")
    return f"event: done\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)}\n\n"


def _segment_windows(start: date, end: date, n_segments: int) -> list[tuple[date, date]]:
    """分段稳定性 (同参数顺序切段) 的窗口切分: 1~12 段、每段 ≥30 天, 不重叠铺满区间。"""
    n_segments = max(1, min(12, int(n_segments)))
    total = (end - start).days + 1
    fold_len = total // n_segments
    if fold_len < 30:
        raise ValueError(f"窗口过短: {n_segments} 窗每窗仅 {fold_len} 天(<30)")
    out = []
    cur = start
    for i in range(n_segments):
        fold_end = end if i == n_segments - 1 else cur + timedelta(days=fold_len - 1)
        out.append((cur, fold_end))
        cur = fold_end + timedelta(days=1)
    return out


# ================================================================
# 策略回测
# ================================================================

# provider 解析惯例同 services/kline_sync._get_data_provider: 进程内按名缓存,
# 避免每次请求新建 FQuantProvider 反复建立 fstore 长连接。
_PROVIDER_CACHE: dict[str, Any] = {}
_PROVIDER_CACHE_LOCK = threading.Lock()


def _get_data_provider(capability: str):
    """按 capability 解析当前配置的 provider (进程内按名缓存单例)。"""
    from app.data_providers.registry import get_active_provider_name, get_provider

    name = get_active_provider_name(capability)
    with _PROVIDER_CACHE_LOCK:
        provider = _PROVIDER_CACHE.get(name)
        if provider is None:
            provider = get_provider(name)
            _PROVIDER_CACHE[name] = provider
        return provider


def _strategy_listing_dates():
    """上市天数门控 (B6) 的 symbol/listing_date 两列表。

    get_stock_reference_flags 是 provider 特有方法 (不在 base 契约), 用
    getattr 防御; provider 不可用 / 查询失败 / 空 df / 缺列一律返回 None —
    service 侧已有显式告警分支, 这里不伪造门控数据。
    """
    try:
        provider = _get_data_provider("daily")
    except Exception as e:  # noqa: BLE001
        logger.warning("上市天数门控: 数据 provider 不可用, 门控不生效 (%s)", e)
        return None
    flags_fn = getattr(provider, "get_stock_reference_flags", None)
    if not callable(flags_fn):
        logger.warning("上市天数门控: 当前 provider 不提供上市日期, 门控不生效")
        return None
    try:
        df = flags_fn()
    except Exception as e:  # noqa: BLE001
        logger.warning("上市天数门控: 上市日期查询失败, 门控不生效 (%s)", e)
        return None
    if df is None or df.is_empty():
        logger.warning("上市天数门控: 上市日期表为空, 门控不生效")
        return None
    if not {"symbol", "listing_date"}.issubset(df.columns):
        logger.warning("上市天数门控: 上市日期表缺少 symbol/listing_date 列, 门控不生效")
        return None
    return df.select("symbol", "listing_date")


def _strategy_backtest_config(
    req: StrategyBacktestRequest,
    start: date,
    end: date,
    *,
    params: dict | None = None,
):
    """StrategyBacktestRequest → StrategyBacktestConfig 的唯一透传点。

    strategy_run / robustness / 诊断端点共用; 新增字段只在这里接一次,
    避免多处构造口径漂移。params 提供时覆盖 req.params (robustness 扰动用)。
    """
    from app.backtest.strategy import StrategyBacktestConfig

    return StrategyBacktestConfig(
        strategy_id=req.strategy_id,
        symbols=req.symbols if req.symbols else None,
        start=start,
        end=end,
        params=req.params if params is None else params,
        overrides=req.overrides,
        matching=req.matching,
        entry_fill=req.entry_fill,
        exit_fill=req.exit_fill,
        fees_pct=req.fees_pct,
        stamp_tax_pct=(req.stamp_tax_pct if req.stamp_tax_pct is not None else 0.0005),
        slippage_bps=req.slippage_bps,
        max_positions=req.max_positions,
        max_exposure_pct=req.max_exposure_pct,
        initial_capital=req.initial_capital,
        position_sizing=req.position_sizing,
        mode=req.mode,
        holding_days=req.holding_days,
        regime_filter=req.regime_filter,
        benchmark_symbol=req.benchmark_symbol,
        risk_free_rate=req.risk_free_rate,
        max_participation_pct=req.max_participation_pct,
        participation_volume_window=req.participation_volume_window,
        min_listed_days=req.min_listed_days,
    )


def _curve_date_iso(raw) -> str | None:
    """曲线日期统一为 ISO 字符串 (date/datetime/str); 无法识别返回 None。"""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    if isinstance(raw, str) and raw:
        return raw
    return None


def _reject_full_mode(analysis: str) -> None:
    """全量独立候选执行的曲线按退出事件日采样, 无日频语义的分析一律 422。"""
    raise HTTPException(
        status_code=422,
        detail=(
            "全量独立候选执行的曲线按退出事件日采样，不支持以日频收益为前提的"
            f"{analysis}；请使用仓位模拟。"
        ),
    )



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
    # A 股印花税 (仅卖出单边): 默认 0.0005 = 万分之五 (2023-08-28 起);
    # None 时由后端 StrategyBacktestConfig 默认值兜底。
    stamp_tax_pct: float | None = Field(default=None, ge=0.0, le=0.01)
    slippage_bps: float = 5.0
    max_positions: int = 10
    max_exposure_pct: float = 1.0
    initial_capital: float = 1_000_000.0
    position_sizing: Literal[
        "equal", "score_weight", "equal_vol", "risk_parity", "mean_variance", "max_diversification",
    ] = "equal"
    mode: Literal["position", "full"] = "position"
    holding_days: int = 5
    regime_filter: dict | None = None  # {states: [...], min_score?: float}
    benchmark_symbol: Literal[
        "000001.INDEX", "000300.INDEX", "000905.INDEX", "000852.INDEX",
    ] = "000001.INDEX"
    risk_free_rate: float = Field(default=0.0, gt=-1.0, le=1.0)
    # A1 量能参与率 + B6 上市天数门控透传: max_participation_pct=None 关闭
    # 量能约束; min_listed_days=0 关闭门控 (启用时经 provider 取上市日期,
    # 不可用则告警并跳过门控, 不伪造)。
    max_participation_pct: float | None = Field(default=None, gt=0.0, le=1.0)
    participation_volume_window: int = Field(default=5, ge=1, le=60)
    min_listed_days: int = Field(default=0, ge=0, le=3650)

@router.post("/strategy/run")
def strategy_run(req: StrategyBacktestRequest, request: Request):
    """策略回测 — 复用 StrategyDef 体系做全周期回测。"""
    from app.backtest.strategy import StrategyBacktestService

    engine = _get_engine(request)
    strategy_engine = request.app.state.strategy_engine
    svc = StrategyBacktestService(engine, strategy_engine)

    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    cfg = _strategy_backtest_config(req, start, end)
    # B6 上市天数门控: 仅在启用时取上市日期表; provider 不可用时传 None,
    # service 侧显式告警并跳过门控 (不伪造)。
    result = svc.run(
        cfg,
        listing_dates=_strategy_listing_dates() if req.min_listed_days > 0 else None,
    )
    payload = _attach_run_provenance(
        json_safe(asdict(result)),
        request,
        start=start,
        end=end,
        symbols=req.symbols if req.symbols else None,
    )
    if not payload.get("error"):
        payload["persisted"] = _persist_backtest_run(
            request,
            _run_from_strategy_payload(
                payload,
                kind=_strategy_run_kind(payload.get("strategy_info") or {}),
            ),
            payload,
        )
    return _attach_methodology(payload, "backtest")


class RobustnessRequest(StrategyBacktestRequest):
    n_folds: int = 4
    bootstrap: bool = True
    mc_permutation: bool = False
    n_boot: int = Field(default=1000, ge=1, le=10000)
    n_perm: int = Field(default=1000, ge=1, le=10000)
    seed: int | None = Field(default=None, ge=0, le=(1 << 63) - 1)
    parameter_perturbation: bool = True
    perturbation_pct: float = Field(default=0.1, gt=0.0, le=0.5)
    max_perturbed_params: int = Field(default=6, ge=1, le=8)
    walk_forward_enabled: bool = False


@router.post("/strategy/robustness")
def strategy_robustness(req: RobustnessRequest, request: Request):
    if req.mode == "full":
        raise HTTPException(
            status_code=422,
            detail=(
                "全量独立候选执行的曲线按退出事件日采样，不支持以日频收益为前提的"
                "分段、Bootstrap、置换或 Walk-Forward 稳健性分析；请使用仓位模拟。"
            ),
        )
    from app.backtest import robustness as rb
    from app.backtest.strategy import StrategyBacktestService

    engine = _get_engine(request)
    svc = StrategyBacktestService(engine, request.app.state.strategy_engine)
    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)
    try:
        windows = _segment_windows(start, end, req.n_folds)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # B6 门控与 strategy_run 同口径: 折窗口/扰动场景共享同一份上市日期表。
    listing_dates = _strategy_listing_dates() if req.min_listed_days > 0 else None

    def run_one(s: date, e: date, *, params: dict | None = None):
        return svc.run(
            _strategy_backtest_config(
                req, s, e,
                params=req.params if params is None else params,
            ),
            listing_dates=listing_dates,
        )

    full = run_one(start, end)
    if full.error:
        raise HTTPException(status_code=400, detail=full.error)
    data_version_value = request.app.state.repo.enriched_latest_date()
    data_version = data_version_value.isoformat() if data_version_value else None
    seed = (
        req.seed
        if req.seed is not None
        else _derive_random_seed(
            req.model_dump(mode="json", exclude={"seed"}),
            data_version,
        )
    )
    folds = []
    for s, e in windows:
        r = run_one(s, e)
        folds.append({"start": s.isoformat(), "end": e.isoformat(), "stats": r.stats, "error": r.error})

    robustness_context = MetricContext("daily", risk_free_rate=req.risk_free_rate)
    rets = rb.returns_from_equity_curve(full.equity_curve)
    robustness = {
        "segment_stability": {
            "folds": folds,
            "summary": rb.segment_stability_summary([f for f in folds if not f["error"]]),
        },
        "exit_breakdown": rb.exit_reason_breakdown(full.trades),
    }
    # 扰动邻域: parameter_perturbation 展示与 Walk-Forward 候选共用同一份有界候选
    perturbation_cases: list[dict] = []
    if req.parameter_perturbation:
        strategy_def = request.app.state.strategy_engine.get(req.strategy_id)
        perturbation_cases = rb.parameter_perturbations(
            list(strategy_def.meta.get("params") or []),
            req.params,
            fraction=req.perturbation_pct,
            max_params=req.max_perturbed_params,
        )
        case_results = []
        for case in perturbation_cases:
            varied_params = dict(req.params or {})
            varied_params[case["param"]] = case["value"]
            result = run_one(start, end, params=varied_params)
            case_results.append({
                **case,
                "stats": {
                    key: (result.stats or {}).get(key)
                    for key in ("total_return", "annual_return", "sharpe", "max_drawdown", "win_rate", "n_trades")
                },
                "error": result.error,
            })
        robustness["parameter_perturbation"] = {
            "fraction": req.perturbation_pct,
            "baseline": {
                key: (full.stats or {}).get(key)
                for key in ("total_return", "annual_return", "sharpe", "max_drawdown", "win_rate", "n_trades")
            },
            "cases": case_results,
            "reason": None if case_results else "策略未声明可扰动的数值参数",
        }
    # 严格 Walk-Forward (显式 opt-in): 每折训练窗内对全部候选训练 → 冻结参数 →
    # 仅 OOS 窗运行一次; OOS 不参与选择。未启用时不执行任何额外回测, 返回
    # enabled=false 的结构化空块 + warning; 启用时在 API 层按执行预算确定性
    # 截断候选 (baseline 最先), 训练+OOS 的额外回测总数有数学上界
    # (n_folds × (effective+1) ≤ n_folds × ⌊budget/n_folds⌋ ≤ 预算上限)。
    if not req.walk_forward_enabled:
        walk_forward = rb.disabled_walk_forward(context=robustness_context)
    else:
        wf_requested = rb.walk_forward_candidates(
            req.params,
            perturbation_cases[: 2 * req.max_perturbed_params],
        )
        wf_plan = rb.walk_forward_fold_plan(start, end, req.n_folds)
        budgeted = rb.cap_walk_forward_candidates(wf_requested, len(wf_plan))
        if wf_plan and budgeted["candidates"]:
            walk_forward = rb.run_walk_forward(
                wf_plan,
                budgeted["candidates"],
                run_one,
                base_params=req.params,
                context=robustness_context,
            )
        else:
            walk_forward = rb.empty_walk_forward(req.n_folds, context=robustness_context)
        walk_forward.update({
            "enabled": True,
            "requested_candidates": budgeted["requested_candidates"],
            "effective_candidates": budgeted["effective_candidates"],
            "max_executions": budgeted["max_executions"],
            "warning": budgeted["warning"] or walk_forward.get("warning"),
        })
    robustness["walk_forward"] = walk_forward
    if req.bootstrap and len(rets) >= 2:
        robustness["bootstrap"] = rb.bootstrap_sharpe_ci(
            rets,
            n_boot=req.n_boot,
            seed=seed,
            context=robustness_context,
        )
    if req.mc_permutation and len(rets) >= 2:
        robustness["mc_permutation"] = rb.mc_permutation_pvalue(
            rets,
            n_perm=req.n_perm,
            seed=seed,
            context=robustness_context,
        )
    # A3 交易级 Bootstrap 净值带: 逐笔收益分布的诊断口径 (顺序无关的单仓位
    # 逐笔等权复利, 不是账户净值, 见模块口径警示); 成交 < 10 笔时 fail-closed
    # 置 None, 不调用。
    if len(full.trades or []) >= 10:
        robustness["trade_equity_band"] = rb.trade_bootstrap_equity_band(
            [t.get("pnl_pct") for t in full.trades if isinstance(t, dict)],
            n_boot=req.n_boot,
            seed=seed,
        )
    else:
        robustness["trade_equity_band"] = None
    robustness["random_seed"] = seed

    # 短区间等 Walk-Forward 边界进入响应 warnings, 与块内 warning 一致可见
    wf_warning = walk_forward.get("warning")


    # 响应 payload 无观测序列: 覆盖必须取 full.equity_curve 的实际观测日期,
    # 不得把请求 end 冒充数据覆盖; 响应与持久化 Run 共用同一 coverage。
    coverage = _observed_coverage({"equity_curve": full.equity_curve}, start, end)
    payload = _attach_run_provenance(
        {
            "run_id": full.run_id,
            "full_stats": full.stats,
            "random_seed": seed,
            **robustness,
        },
        request,
        start=start,
        end=end,
        symbols=req.symbols if req.symbols else None,
        risk_free_rate=req.risk_free_rate,
        random_seed=seed,
        coverage=coverage,
        warnings=[wf_warning] if wf_warning else None,
    )
    # 不可变完整 Run: 曲线/交易来自 full run, robustness 摘要并入 stats。
    run_payload = _attach_run_provenance(
        json_safe(asdict(full)),
        request,
        start=start,
        end=end,
        symbols=req.symbols if req.symbols else None,
        random_seed=seed,
        coverage=coverage,
    )
    run_payload["stats"] = {**full.stats, "robustness": robustness}
    payload["persisted"] = _persist_backtest_run(
        request,
        _run_from_strategy_payload(
            run_payload,
            kind=_strategy_run_kind(full.strategy_info or {}),
            random_seed=seed,
        ),
        payload,
    )
    return _attach_methodology(payload, "backtest")


# ================================================================
# 策略诊断端点 (regime 分桶 / 成本敏感性 / 风格归因): 不持久化 Run
# ================================================================

@router.post("/strategy/regime-breakdown")
def strategy_regime_breakdown(req: StrategyBacktestRequest, request: Request):
    """市场状态条件表现 — 按基准牛/熊 × 高/低波动四桶统计策略表现。

    先完整执行一次仓位模拟回测, 再对策略净值与基准净值做事后分组
    (vol 阈值取基准全样本中位数, 含轻度前视, 仅用于分组解释)。请求即
    StrategyBacktestRequest, 无额外字段。诊断端点, 结果不持久化为 Run。
    """
    if req.mode == "full":
        _reject_full_mode("市场状态分桶分析")
    from app.backtest.regime_breakdown import regime_breakdown
    from app.backtest.strategy import StrategyBacktestService

    engine = _get_engine(request)
    svc = StrategyBacktestService(engine, request.app.state.strategy_engine)
    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    cfg = _strategy_backtest_config(req, start, end)
    result = svc.run(
        cfg,
        listing_dates=_strategy_listing_dates() if req.min_listed_days > 0 else None,
    )
    if result.error:
        raise HTTPException(status_code=400, detail=result.error)

    # benchmark_curve 为 {date, close}; regime_breakdown 统一消费 {date, value},
    # 日期统一转 ISO 字符串与策略净值侧对齐。
    strategy_curve = [
        {"date": _curve_date_iso(p.get("date")), "value": p.get("value")}
        for p in (result.equity_curve or [])
        if isinstance(p, dict)
    ]
    benchmark_curve = [
        {"date": _curve_date_iso(p.get("date")), "value": p.get("close")}
        for p in (result.benchmark_curve or [])
        if isinstance(p, dict)
    ]
    regime = regime_breakdown(
        strategy_curve,
        benchmark_curve,
        MetricContext("daily", risk_free_rate=req.risk_free_rate),
    )
    return json_safe({
        "regime": regime,
        "run_id": result.run_id,
        "note": (
            "按基准 60 日均值分牛熊、20 日滚动波动中位数分高低波动的事后分组；"
            "vol 阈值含轻度前视，仅用于分组解释，不构成交易信号。"
        ),
    })


class CostSensitivityRequest(StrategyBacktestRequest):
    # 负数倍数会翻转成本方向且无业务含义, 逐项 >= 0 校验; 去重/补基线
    # (1.0) 由 cost_sensitivity 模块归一化, rows 与归一化后档位对齐。
    multipliers: list[Annotated[float, Field(ge=0.0)]] = Field(
        default=[0.0, 0.5, 1.0, 2.0, 5.0], min_length=2, max_length=6,
    )


@router.post("/strategy/cost-sensitivity")
def strategy_cost_sensitivity(req: CostSensitivityRequest, request: Request):
    """成本敏感性 — 同一策略在不同交易成本倍数下逐档完整重跑的对比。

    ⚠️ 服务端耗时与档数成正比 (默认 5 档 = 5 次完整回测), 显著慢于
    /strategy/run。mode=full 允许: 成本对独立候选执行同样有意义, 候选口径
    下不可用的时序指标由模块置 null。诊断端点, 结果不持久化。
    """
    from app.backtest.cost_sensitivity import run_cost_sensitivity
    from app.backtest.strategy import StrategyBacktestService

    engine = _get_engine(request)
    svc = StrategyBacktestService(engine, request.app.state.strategy_engine)
    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    cfg = _strategy_backtest_config(req, start, end)
    listing_dates = _strategy_listing_dates() if req.min_listed_days > 0 else None
    results_in_order: list = []

    def run_fn(scenario):
        r = svc.run(scenario, listing_dates=listing_dates)
        results_in_order.append(r)
        return r

    t0 = time.perf_counter()
    sensitivity = run_cost_sensitivity(run_fn, cfg, req.multipliers)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 3)
    # 基线 run_id: run_cost_sensitivity 按升序倍数逐档恰好调用一次 run_fn,
    # 归一化后的 multipliers 与 results_in_order 按序一一对应 (1.0 必在档中)。
    run_id_baseline = None
    for m, r in zip(sensitivity["multipliers"], results_in_order):
        if m == 1.0:
            if getattr(r, "error", None):
                raise HTTPException(status_code=400, detail=str(r.error))
            run_id_baseline = getattr(r, "run_id", None)
            break
    return json_safe({
        "cost_sensitivity": sensitivity,
        "run_id_baseline": run_id_baseline,
        "elapsed_ms": elapsed_ms,
    })


@router.post("/strategy/style-attribution")
def strategy_style_attribution(req: StrategyBacktestRequest, request: Request):
    """风格归因 — 策略日收益对面板内自建 SMB/UMD/LMV 因子的 OLS 回归。

    面板用 engine.load_panel(该次回测 symbols, 请求区间) 重建 (复用引擎
    缓存); 因子有效日 < 120 或对齐样本不足时 style_attribution 为 null,
    style_factor_meta 说明原因 (fail-closed, 不伪造归因)。诊断端点,
    结果不持久化。
    """
    if req.mode == "full":
        _reject_full_mode("风格因子归因")
    from app.backtest.robustness import returns_from_equity_curve
    from app.backtest.style_factors import build_style_factor_returns, style_attribution
    from app.backtest.strategy import StrategyBacktestService

    engine = _get_engine(request)
    svc = StrategyBacktestService(engine, request.app.state.strategy_engine)
    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    cfg = _strategy_backtest_config(req, start, end)
    result = svc.run(
        cfg,
        listing_dates=_strategy_listing_dates() if req.min_listed_days > 0 else None,
    )
    if result.error:
        raise HTTPException(status_code=400, detail=result.error)

    # 面板向前扩 warmup: mom_252_21 需 253 观测、vol_60 需 60, 不足时
    # UMD 结构性全 null (短窗口归因静默失效的根因)。因子序列仍按
    # 日期与策略收益对齐, 扩展窗口不改变对齐样本。
    panel_start = start - timedelta(days=470)
    panel = engine.load_panel(cfg.symbols, panel_start, end)
    factor_df, meta = build_style_factor_returns(panel)
    attribution = None
    if factor_df is not None:
        # 收益序列第 i 个对应 equity_curve 第 i+1 天: 传真实发生日,
        # 与因子日按日期键对齐 (位置错位会引入一天滞后偏差)。
        strategy_dates = [
            str(row.get("date"))[:10]
            for row in result.equity_curve[1:]
            if row.get("date")
        ]
        attribution = style_attribution(
            returns_from_equity_curve(result.equity_curve),
            strategy_dates,
            factor_df,
            MetricContext("daily", risk_free_rate=req.risk_free_rate),
        )
    return json_safe({
        "style_attribution": attribution,
        "style_factor_meta": meta,
        "run_id": result.run_id,
    })


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


def _parse_json_object_param(raw: str | None, name: str) -> dict | None:
    """开流前解析 params/overrides/regime_filter: 非法 JSON/非对象必须结构化 422,
    不能留到 StreamingResponse 开始后在生成器里断流。"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"{name} 不是合法 JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail=f"{name} 必须是 JSON 对象")
    return parsed


def _make_job_key(
    strategy_id: str, symbols: str | None, start: str | None, end: str | None,
    matching: str, entry_fill: str | None, exit_fill: str | None,
    fees_pct: float, stamp_tax_pct: float, slippage_bps: float,
    max_positions: int, max_exposure_pct: float, initial_capital: float, position_sizing: str,
    params: str | None, overrides: str | None,
    mode: str = "position", holding_days: int = 5, regime_filter: str | None = None,
    benchmark_symbol: str = "000001.INDEX", risk_free_rate: float = 0.0,
    max_participation_pct: float | None = None, participation_volume_window: int = 5,
    min_listed_days: int = 0,
) -> str:
    raw = f"{strategy_id}|{symbols}|{start}|{end}|{matching}|{entry_fill}|{exit_fill}|{fees_pct}|{stamp_tax_pct}|{slippage_bps}|{max_positions}|{max_exposure_pct}|{initial_capital}|{position_sizing}|{params}|{overrides}|{mode}|{holding_days}|{regime_filter}|{benchmark_symbol}|{risk_free_rate}|{max_participation_pct}|{participation_volume_window}|{min_listed_days}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def _make_factor_job_key(
    factor_name: str,
    symbols: str | None,
    start: str | None,
    end: str | None,
    all_history: bool,
    n_groups: int,
    rebalance: str,
    weight: str,
    fees_pct: float,
    slippage_bps: float,
    risk_free_rate: float,
) -> str:
    """因子 SSE 的稳定任务键；命名空间与策略任务隔离。"""
    raw = (
        f"{factor_name}|{symbols}|{start}|{end}|{all_history}|{n_groups}|"
        f"{rebalance}|{weight}|{fees_pct}|{slippage_bps}|{risk_free_rate}"
    )
    return f"factor:{hashlib.md5(raw.encode()).hexdigest()[:12]}"


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
    # A 股印花税 (仅卖出单边): None = 用 StrategyBacktestConfig 默认 (0.0005)。
    stamp_tax_pct: float | None = Query(default=None, ge=0.0, le=0.01),
    slippage_bps: float = 5.0,
    max_positions: int = 10,
    max_exposure_pct: float = 1.0,
    initial_capital: float = 1_000_000.0,
    position_sizing: str = "equal",
    params: str | None = None,
    overrides: str | None = None,
    mode: Literal["position", "full"] = "position",
    holding_days: int = 5,
    regime_filter: str | None = None,
    benchmark_symbol: Literal[
        "000001.INDEX", "000300.INDEX", "000905.INDEX", "000852.INDEX",
    ] = "000001.INDEX",
    risk_free_rate: float = Query(default=0.0, gt=-1.0, le=1.0),
    max_participation_pct: float | None = Query(default=None, gt=0.0, le=1.0),
    participation_volume_window: int = Query(default=5, ge=1, le=60),
    min_listed_days: int = Query(default=0, ge=0, le=3650),
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

    # 参数在开流前解析/校验 (引擎创建/任务注册之前): 非法输入得到结构化 4xx,
    # 而不是 500 或 SSE 开始后断流。
    try:
        end_date = date.fromisoformat(end) if end else date.today()
        start_date = date.fromisoformat(start) if start else None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"start/end 需要 ISO 日期 (YYYY-MM-DD): {e}") from e
    if start_date is None:
        # 空 start = 全部历史: 用本地最早日K日期, 查不到再回退到默认窗口
        earliest = request.app.state.repo.earliest_daily_date()
        start_date = earliest or (end_date - timedelta(days=FACTOR_DEFAULT_DAYS))
    params_obj = _parse_json_object_param(params, "params")
    overrides_obj = _parse_json_object_param(overrides, "overrides")
    regime_filter_obj = _parse_json_object_param(regime_filter, "regime_filter")

    # 范围保护必须在注册 job 前返回，否则被拒绝的首次请求会留下永不完成的
    # 任务，使相同 query 的后续订阅者永久等待。
    if settings.backtest_range_guard and (
        (end_date - start_date).days + 1 > BACKTEST_MAX_SERVER_DAYS
    ):
        async def _guard_error():
            yield (
                "event: error\ndata: "
                f"{json.dumps({'message': BACKTEST_SERVER_GUARD_MESSAGE}, ensure_ascii=False)}\n\n"
            )

        return StreamingResponse(_guard_error(), media_type="text/event-stream")

    engine = _get_engine(request)
    strategy_engine = request.app.state.strategy_engine
    svc = StrategyBacktestService(engine, strategy_engine)

    job_key = _make_job_key(
        strategy_id, symbols, start, end,
        matching, entry_fill, exit_fill,
        fees_pct, stamp_tax_pct if stamp_tax_pct is not None else 0.0005, slippage_bps, max_positions, max_exposure_pct, initial_capital, position_sizing,
        params, overrides,
        mode, holding_days, regime_filter, benchmark_symbol, risk_free_rate,
        max_participation_pct, participation_volume_window, min_listed_days,
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

        # 如果是新任务, 启动回测线程
        if is_new and not job.done:
            cfg = StrategyBacktestConfig(
                strategy_id=strategy_id,
                symbols=[s.strip() for s in symbols.split(",") if s.strip()] if symbols else None,
                start=start_date,
                end=end_date,
                params=params_obj,
                overrides=overrides_obj,
                matching=matching,
                entry_fill=entry_fill,
                exit_fill=exit_fill,
                fees_pct=fees_pct,
                stamp_tax_pct=stamp_tax_pct if stamp_tax_pct is not None else 0.0005,
                slippage_bps=slippage_bps,
                max_positions=int(max_positions),
                max_exposure_pct=float(max_exposure_pct),
                initial_capital=float(initial_capital),
                position_sizing=position_sizing,
                mode=mode,
                holding_days=int(holding_days),
                regime_filter=regime_filter_obj,
                benchmark_symbol=benchmark_symbol,
                risk_free_rate=risk_free_rate,
                max_participation_pct=max_participation_pct,
                participation_volume_window=participation_volume_window,
                min_listed_days=min_listed_days,
            )

            # 上市日期表在工作线程内取 (DuckDB 查询不阻塞事件循环);
            # min_listed_days=0 时跳过。
            def _run_backtest():
                try:
                    listing_dates = (
                        _strategy_listing_dates() if min_listed_days > 0 else None
                    )
                    result = svc.run(
                        cfg, lambda d: job.progress.append(d), job.cancel_event,
                        listing_dates=listing_dates,
                    )
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
                # 新订阅者先重放全部已记录的进度；即使工作线程已经结束，也不能
                # 让 done 抢在缓冲进度前面而丢失任务生命周期。
                prog_list = job.progress
                while cursor < len(prog_list):
                    msg = prog_list[cursor]
                    cursor += 1
                    yield f"event: progress\ndata: {json.dumps(json_safe(msg), ensure_ascii=False, allow_nan=False, default=str)}\n\n"

                # 已完成: 在进度重放后推送最终结果/错误并退出。
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

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.get("/factor/stream")
async def factor_stream(
    request: Request,
    factor_name: str,
    symbols: str | None = None,
    start: str | None = None,
    end: str | None = None,
    n_groups: int = Query(default=5, ge=2, le=10),
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly",
    weight: Literal["equal", "factor_weight"] = "equal",
    fees_pct: float = 0.0002,
    slippage_bps: float = 5.0,
    risk_free_rate: float = Query(default=0.0, gt=-1.0, le=1.0),
):
    """SSE 因子回测：共享任务、进度回放、显式取消与完成结果持久化。"""
    from app.backtest.factor import FactorBacktestService

    start_text = start.strip() if start else None
    end_text = end.strip() if end else None
    try:
        start_date = date.fromisoformat(start_text) if start_text else None
        end_date = date.fromisoformat(end_text) if end_text else None
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"start/end 需要 ISO 日期 (YYYY-MM-DD): {e}",
        ) from e

    start_is_all_history = start_date is None and "start" in request.query_params
    cfg = _build_factor_config(
        factor_name=factor_name,
        symbols=[s.strip() for s in symbols.split(",") if s.strip()] if symbols else None,
        start=start_date,
        end=end_date,
        start_is_all_history=start_is_all_history,
        n_groups=n_groups,
        rebalance=rebalance,
        weight=weight,
        fees_pct=fees_pct,
        slippage_bps=slippage_bps,
        risk_free_rate=risk_free_rate,
    )
    # 同策略 SSE: 必须在 job 表写入前拒绝超长区间，避免无工作线程的幽灵任务。
    if settings.backtest_range_guard and (
        (cfg.end - cfg.start).days + 1 > BACKTEST_MAX_SERVER_DAYS
    ):
        async def _guard_error():
            yield (
                "event: error\ndata: "
                f"{json.dumps({'message': BACKTEST_SERVER_GUARD_MESSAGE}, ensure_ascii=False)}\n\n"
            )

        return StreamingResponse(_guard_error(), media_type="text/event-stream")

    engine = _get_engine(request)
    svc = FactorBacktestService(engine)
    job_key = _make_factor_job_key(
        cfg.factor_name,
        ",".join(cfg.symbols) if cfg.symbols else None,
        cfg.start.isoformat(),
        cfg.end.isoformat(),
        start_is_all_history,
        cfg.n_groups,
        cfg.rebalance,
        cfg.weight,
        cfg.fees_pct,
        cfg.slippage_bps,
        cfg.risk_free_rate,
    )
    _cleanup_stale_jobs()
    with _jobs_lock:
        job = _running_jobs.get(job_key)
        if job is None:
            job = _BacktestJob(job_key)
            _running_jobs[job_key] = job
            is_new = True
        else:
            is_new = False

    async def event_generator():

        if is_new and not job.done:
            def _run_factor():
                try:
                    result = svc.run(
                        cfg,
                        lambda detail: job.progress.append(detail),
                        job.cancel_event,
                    )
                    job.result = result
                except Exception as e:  # noqa: BLE001
                    logger.exception("因子回测任务异常")
                    job.error = str(e)
                finally:
                    job.done = True
                    job.finish_ts = time.time()

            threading.Thread(target=_run_factor, daemon=True).start()

        cursor = 0
        tick = 0
        try:
            while True:
                # done 前先排空进度历史，保证新订阅者即便连接到已完成任务，也能
                # 得到完整、按序的生命周期回放。
                prog_list = job.progress
                while cursor < len(prog_list):
                    msg = prog_list[cursor]
                    cursor += 1
                    yield (
                        "event: progress\ndata: "
                        f"{json.dumps(json_safe(msg), ensure_ascii=False, allow_nan=False, default=str)}\n\n"
                    )

                if job.done:
                    if job.error:
                        yield f"event: error\ndata: {json.dumps({'message': job.error}, ensure_ascii=False)}\n\n"
                    elif job.result is not None:
                        yield _factor_stream_done_event(request, job.result)
                    return

                tick += 1
                if tick % 4 == 0 and await request.is_disconnected():
                    break

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
        float(_get("stamp_tax_pct", "0.0005")),
        float(_get("slippage_bps", "5")),
        int(_get("max_positions", "10")),
        float(_get("max_exposure_pct", "1")),
        float(_get("initial_capital", "1000000")),
        _get("position_sizing", "equal"),
        _get("params") or None,
        _get("overrides") or None,
        _get("mode", "position"),
        int(_get("holding_days", "5")),
        _get("regime_filter") or None,
        _get("benchmark_symbol", "000001.INDEX"),
        float(_get("risk_free_rate", "0")),
    )
    job = _running_jobs.get(job_key)
    if job and not job.done:
        job.cancel_event.set()
        return {"ok": True}
    return {"ok": False, "message": "任务不存在或已完成"}

@router.post("/factor/cancel")
async def factor_cancel(request: Request):
    """取消匹配配置的运行中因子任务。"""
    from urllib.parse import parse_qs

    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail="请求体必须是 JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="请求体必须是 JSON 对象")

    p = parse_qs(str(body.get("qs", "")), keep_blank_values=True)

    def _get(key: str, default: str = "") -> str:
        return p.get(key, [default])[0]

    try:
        start_text = _get("start") or None
        end_text = _get("end") or None
        start_date = date.fromisoformat(start_text) if start_text else None
        end_date = date.fromisoformat(end_text) if end_text else None
        start_is_all_history = start_date is None and "start" in p
        cfg = _build_factor_config(
            factor_name=_get("factor_name"),
            symbols=[s.strip() for s in _get("symbols").split(",") if s.strip()] or None,
            start=start_date,
            end=end_date,
            start_is_all_history=start_is_all_history,
            n_groups=int(_get("n_groups", "5")),
            rebalance=_get("rebalance", "monthly"),
            weight=_get("weight", "equal"),
            fees_pct=float(_get("fees_pct", "0.0002")),
            slippage_bps=float(_get("slippage_bps", "5")),
            risk_free_rate=float(_get("risk_free_rate", "0")),
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"取消参数无效: {e}") from e

    job_key = _make_factor_job_key(
        cfg.factor_name,
        ",".join(cfg.symbols) if cfg.symbols else None,
        cfg.start.isoformat(),
        cfg.end.isoformat(),
        start_is_all_history,
        cfg.n_groups,
        cfg.rebalance,
        cfg.weight,
        cfg.fees_pct,
        cfg.slippage_bps,
        cfg.risk_free_rate,
    )
    job = _running_jobs.get(job_key)
    if job and not job.done:
        job.cancel_event.set()
        return {"ok": True}
    return {"ok": False, "message": "任务不存在或已完成"}


# ================================================================
# BacktestRun 实验闭环: 列表/读取/比较/导出/复跑/收藏标签/删除
# ================================================================

class RunPatchRequest(BaseModel):
    """PATCH 仅允许 favorite/label, 其余字段 422。"""
    model_config = ConfigDict(extra="forbid")

    favorite: bool | None = None
    label: str | None = Field(default=None, max_length=200)


class RunCompareRequest(BaseModel):
    run_ids: list[str] = Field(..., min_length=2, max_length=4)


def _get_run_or_404(store: BacktestRunStore, run_id: str) -> BacktestRun:
    try:
        return store.get(run_id)
    except RunIdError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail="run not found") from e


@router.get("/runs")
def list_runs(
    request: Request,
    kind: str | None = None,
    favorite: bool | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """轻量摘要列表; 支持kind/favorite/query/limit/offset过滤。"""
    if kind is not None and kind not in RUN_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {kind}")
    return _run_store(request).list_runs(
        kind=kind, favorite=favorite, query=query, limit=limit, offset=offset,
    )


@router.post("/runs/compare")
def runs_compare(req: RunCompareRequest, request: Request):
    """2~4 个 run 的指标矩阵 + 曲线 + 可比性警告。"""
    store = _run_store(request)
    runs = [_get_run_or_404(store, run_id) for run_id in dict.fromkeys(req.run_ids)]
    if len(runs) < 2:
        raise HTTPException(status_code=400, detail="比较至少需要 2 个不同的 run_id")
    return json_safe(compare_runs(runs))


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    """完整 Run (曲线/交易/因子明细), 刷新/重启后可读取。"""
    return json_safe(_get_run_or_404(_run_store(request), run_id).model_dump())


@router.patch("/runs/{run_id}")
def patch_run(run_id: str, body: RunPatchRequest, request: Request):
    """仅 favorite/label 可变; 旧 run_card 先固化迁移再改。"""
    store = _run_store(request)
    try:
        run = store.patch(
            run_id,
            favorite=body.favorite,
            label=body.label,
        )
    except RunIdError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail="run not found") from e
    except RunTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e)) from e
    return json_safe(run.model_dump())


@router.delete("/runs/{run_id}")
def delete_run(run_id: str, request: Request):
    """仅删除明确的 backtest_runs/{run_id}.json; 旧 run_card 拒绝删除。"""
    try:
        ok = _run_store(request).delete(run_id)
    except RunIdError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LegacyRunCardReadOnly as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="run not found")
    return {"ok": True}


@router.get("/runs/{run_id}/export")
def export_run(run_id: str, request: Request, fmt: str = "json"):
    """JSON 导出完整 Run; CSV 导出 trades (因子 run 导出 group_stats)。"""
    run = _get_run_or_404(_run_store(request), run_id)
    if fmt == "csv":
        try:
            detail_name, data = export_csv(run)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{run.run_id}_{detail_name}.csv"'
            },
        )
    if fmt != "json":
        raise HTTPException(status_code=400, detail=f"unsupported format: {fmt}")
    data = _run_store(request).serialize(run)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{run.run_id}.json"'},
    )


@router.post("/runs/{run_id}/fill-reachability")
def run_fill_reachability(
    run_id: str,
    request: Request,
    sample: int = Query(default=20, ge=1, le=100),
    seed: int = Query(default=0),
):
    """成交可达性诊断 (B7) — 对已持久化 Run 的成交抽样做分钟级价格带抽查。

    逐笔读取成交日分钟线 (单标的单日 09:00-15:30), 计算成交价 ±0.5% 价格
    带内分钟成交额相对交易名义金额的 headroom; 分钟数据读取失败 fail-soft
    计入 no_data, 不中断诊断。仅支持有成交明细的 strategy/composite Run。
    """
    run = _get_run_or_404(_run_store(request), run_id)
    if run.kind not in ("strategy", "composite"):
        raise HTTPException(
            status_code=422,
            detail=f"kind={run.kind} 的 Run 无成交明细，不支持成交可达性诊断",
        )
    trades = run.trades or []
    if not trades:
        raise HTTPException(status_code=422, detail="该 Run 无成交记录，无法做成交可达性诊断")

    import polars as pl
    from app.backtest.fill_reachability import diagnose_fill_reachability

    provider = None
    try:
        provider = _get_data_provider("minute")
    except Exception as e:  # noqa: BLE001
        logger.warning("成交可达性诊断: 分钟数据 provider 不可用 (%s)", e)

    minute_warn_budget = 5  # 分钟读取失败告警上限, 防止逐笔刷屏

    def get_minutes_fn(symbol: str, day: date) -> pl.DataFrame:
        """(symbol, day) → 当日分钟线; 失败返回空 df 并限频告警。"""
        nonlocal minute_warn_budget
        if provider is None:
            return pl.DataFrame()
        try:
            return provider.get_minute(
                [symbol],
                start_time=datetime(day.year, day.month, day.day, 9, 0),
                end_time=datetime(day.year, day.month, day.day, 15, 30),
                asset_type="stock",
            )
        except Exception as e:  # noqa: BLE001
            if minute_warn_budget > 0:
                minute_warn_budget -= 1
                logger.warning(
                    "成交可达性诊断: %s %s 分钟数据读取失败 (%s)", symbol, day, e
                )
            return pl.DataFrame()

    fill = diagnose_fill_reachability(trades, get_minutes_fn, sample=sample, seed=seed)
    return json_safe({"fill_reachability": fill, "run_id": run_id})


@router.post("/runs/{run_id}/rerun")
def rerun_run(run_id: str, request: Request):
    """按原 config 用当前服务重新运行; 生成新 run_id + source_run_id, 不改原 Run。"""
    run = _get_run_or_404(_run_store(request), run_id)
    if run.kind not in RUN_KINDS:  # defensive: model 已限 Literal
        raise HTTPException(status_code=400, detail=f"run kind {run.kind} 不支持复跑")
    cfg = run.config or {}
    if run.kind != "factor" and not cfg.get("strategy_id"):
        raise HTTPException(status_code=400, detail="run config 缺少 strategy_id，无法复跑")
    if run.kind == "factor" and not cfg.get("factor_name"):
        raise HTTPException(status_code=400, detail="run config 缺少 factor_name，无法复跑")

    new_payload, new_kind = _rerun_execute(request, run)
    new_run = (
        _run_from_factor_payload(new_payload)
        if new_kind == "factor"
        else _run_from_strategy_payload(new_payload, kind=new_kind)
    )
    # 新 run: 新 id、记录来源; 数据快照变化时显式警告。
    old_hash = (run.data_snapshot or {}).get("snapshot_hash")
    new_hash = (new_run.data_snapshot or {}).get("snapshot_hash")
    if old_hash and new_hash and old_hash != new_hash:
        new_run.warnings = list(dict.fromkeys(
            [*new_run.warnings, "rerun_data_snapshot_changed: 复跑时数据快照已变化，与原 run 不可直接比较"]
        ))
    new_run.source_run_id = run.run_id
    new_run.run_id = _new_run_id()
    saved = _save_backtest_run(request, new_run)
    if saved is None:
        raise HTTPException(status_code=500, detail="保存复跑结果失败")
    return json_safe(saved.model_dump())


def _new_run_id() -> str:
    import uuid

    return uuid.uuid4().hex[:16]


def _rerun_execute(request: Request, run: BacktestRun) -> tuple[dict, str]:
    """按原 config 调用当前服务重新运行, 返回 (完整payload, kind)。"""
    cfg = run.config or {}
    if run.kind == "factor":
        from app.backtest.factor import FactorBacktestService, FactorConfig

        engine = _get_engine(request)
        svc = FactorBacktestService(engine)
        end = date.fromisoformat(str(cfg["end"])) if cfg.get("end") else date.today()
        start = date.fromisoformat(str(cfg["start"])) if cfg.get("start") else end - timedelta(days=STRATEGY_DEFAULT_DAYS)
        _guard_server_backtest_range(start, end)
        result = svc.run(FactorConfig(
            factor_name=cfg["factor_name"],
            symbols=cfg.get("symbols"),
            start=start,
            end=end,
            n_groups=cfg.get("n_groups", 5),
            rebalance=cfg.get("rebalance", "monthly"),
            weight=cfg.get("weight", "equal"),
            fees_pct=cfg.get("fees_pct", 0.0002),
            slippage_bps=cfg.get("slippage_bps", 5.0),
            risk_free_rate=cfg.get("risk_free_rate", 0.0),
        ))
        payload = _attach_run_provenance(
            json_safe(asdict(result)),
            request,
            start=start,
            end=end,
            symbols=cfg.get("symbols"),
            return_frequency=cfg.get("rebalance", "monthly"),
            risk_free_rate=cfg.get("risk_free_rate", 0.0),
        )
        if payload.get("error"):
            raise HTTPException(status_code=400, detail=str(payload["error"]))
        return payload, "factor"

    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

    engine = _get_engine(request)
    svc = StrategyBacktestService(engine, request.app.state.strategy_engine)
    end = date.fromisoformat(str(cfg["end"])) if cfg.get("end") else date.today()
    start = date.fromisoformat(str(cfg["start"])) if cfg.get("start") else end - timedelta(days=FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)
    result = svc.run(StrategyBacktestConfig(
        strategy_id=cfg["strategy_id"],
        symbols=cfg.get("symbols"),
        start=start,
        end=end,
        params=cfg.get("params"),
        overrides=cfg.get("overrides"),
        matching=cfg.get("matching", "open_t+1"),
        fees_pct=cfg.get("fees_pct", 0.0002),
        stamp_tax_pct=cfg.get("stamp_tax_pct", 0.0005),
        slippage_bps=cfg.get("slippage_bps", 5.0),
        max_positions=cfg.get("max_positions", 10),
        max_exposure_pct=cfg.get("max_exposure_pct", 1.0),
        initial_capital=cfg.get("initial_capital", 1_000_000.0),
        position_sizing=cfg.get("position_sizing", "equal"),
        mode=cfg.get("mode", "position"),
        holding_days=cfg.get("holding_days", 5),
        regime_filter=cfg.get("regime_filter"),
        benchmark_symbol=cfg.get("benchmark_symbol", "000001.INDEX"),
        risk_free_rate=cfg.get("risk_free_rate", 0.0),
    ))
    if result.error:
        raise HTTPException(status_code=400, detail=result.error)
    payload = _attach_run_provenance(
        json_safe(asdict(result)),
        request,
        start=start,
        end=end,
        symbols=cfg.get("symbols"),
    )
    return payload, _strategy_run_kind(payload.get("strategy_info") or {})
